"""推送核心：去重 -> 队列 -> 并发 worker -> OneBot -> QQ 群.

HTTP 请求只负责入队（毫秒级返回 202），真正的发送在后台 worker 完成，
避免上游被 QQ 侧的慢响应拖住。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .channels import ChannelAdapter
from .config import Config, GroupTable
from .message import to_plain_text
from .onebot import OneBotError, SendResult
from .util import LOG


class Deduper:
    """带 TTL 的幂等去重：上游重试时同一 id 只推一次."""

    def __init__(self, ttl: float):
        self.ttl = ttl
        self._seen: dict[str, float] = {}
        self._order: deque[str] = deque()

    def check_and_add(self, key: str) -> bool:
        """返回 True 表示这是新 key（应该处理），False 表示重复."""
        if not key or self.ttl <= 0:
            return True
        now = time.monotonic()
        while self._order and now - self._seen.get(self._order[0], now) > self.ttl:
            self._seen.pop(self._order.popleft(), None)
        if key in self._seen:
            return False
        self._seen[key] = now
        self._order.append(key)
        return True


@dataclass
class Job:
    job_id: str
    targets: list[int | str]
    segments: list[dict]
    source: str = "api"
    created_at: float = field(default_factory=time.time)


class JobStore:
    """记录近期的投递结果，供 /status/<job_id> 查询（内存态，重启即清空）."""

    def __init__(self, maxlen: int = 500):
        self._lock = threading.Lock()
        self._entries: deque[dict] = deque(maxlen=maxlen)
        self._index: dict[str, dict] = {}

    def create(self, job_id: str, target, source: str) -> None:
        entry = {
            "job_id": job_id,
            "target": target,
            "source": source,
            "state": "pending",
            "message_id": None,
            "error": "",
            "attempts": 0,
            "created_at": time.time(),
            "finished_at": None,
        }
        with self._lock:
            self._index[self._key(job_id, target)] = entry
            self._entries.append(entry)

    @staticmethod
    def _key(job_id: str, target) -> str:
        return f"{job_id}:{target}"

    def finish(self, job_id: str, target, result: SendResult) -> None:
        with self._lock:
            entry = self._index.get(self._key(job_id, target))
            if entry is None:
                return
            entry["state"] = "sent" if result.ok else "failed"
            entry["message_id"] = result.message_id
            entry["error"] = result.error
            entry["finished_at"] = time.time()
            entry["attempts"] = result.attempts

    def get(self, job_id: str) -> dict | None:
        """汇总同一个 job 下所有目标群的状态."""
        with self._lock:
            entries = [dict(e) for e in self._entries if e["job_id"] == job_id]
        if not entries:
            return None
        done = [e for e in entries if e["state"] != "pending"]
        state = "pending"
        if len(done) == len(entries):
            sent = sum(1 for e in entries if e["state"] == "sent")
            if sent == len(entries):
                state = "sent"
            elif sent == 0:
                state = "failed"
            else:
                state = "partial"
        return {
            "job_id": job_id,
            "state": state,
            "created_at": min(e["created_at"] for e in entries),
            "results": entries,
        }

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return [dict(e) for e in list(self._entries)[-limit:]][::-1]


class PushService:
    def __init__(self, cfg: Config, groups: GroupTable, adapter: ChannelAdapter):
        self.cfg = cfg
        self.groups = groups
        self.adapter = adapter
        self._queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=cfg.queue_maxsize)
        self._deduper = Deduper(cfg.dedup_ttl)
        self._workers: list[asyncio.Task] = []
        self._closed = False
        self._failed_lock = threading.Lock()
        self.jobs = JobStore()
        self.stats = {
            "accepted": 0,
            "sent": 0,
            "failed": 0,
            "duplicate": 0,
            "rejected": 0,
            "enqueue_timeout": 0,
        }

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        if self.cfg.failed_log:
            Path(self.cfg.failed_log).parent.mkdir(parents=True, exist_ok=True)
        for index in range(self.cfg.workers):
            self._workers.append(
                asyncio.create_task(self._worker(index), name=f"qqpush-worker-{index}")
            )
        LOG.info("已启动 %d 个发送 worker，队列上限 %d", self.cfg.workers, self.cfg.queue_maxsize)

    async def stop(self, drain_timeout: float = 15.0) -> None:
        """优雅退出：等队列里的消息发完（最多 drain_timeout 秒）."""
        self._closed = True
        if not self._queue.empty():
            LOG.info("退出中：等待 %d 条待发送消息…", self._queue.qsize())
        try:
            await asyncio.wait_for(self._queue.join(), timeout=drain_timeout)
        except asyncio.TimeoutError:
            LOG.warning("等待超时，剩余 %d 条消息未发送", self._queue.qsize())
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        LOG.info("已停止，统计：%s", self.stats)

    # ---------- 入队 ----------

    async def submit(
        self,
        message,
        *,
        segments: list[dict] | None = None,
        targets: list[str] | None = None,
        source: str = "api",
        dedup_key: str = "",
    ) -> dict:
        """把一条消息投递到队列，返回受理结果（不等待发送完成）."""
        if self._closed:
            return {"accepted": False, "error": "服务正在关闭"}

        key = dedup_key or ""
        if key and not self._deduper.check_and_add(key):
            self.stats["duplicate"] += 1
            LOG.info("命中幂等去重，忽略重复消息 id=%s", key)
            return {"accepted": False, "duplicate": True, "job_id": ""}

        names = [str(t) for t in (targets or [])] or list(self.groups.default_targets)
        if not names:
            self.stats["rejected"] += 1
            return {
                "accepted": False,
                "error": "没有可用的推送目标：请在 groups.json 配置 default_targets 或请求里带 groups",
            }

        resolved: list = []
        unknown: list[str] = []
        unresolved: list[str] = []
        for name in names:
            target = self.adapter.resolve(name)
            if target is None:
                # 区分「没配过这个群」和「配了但还缺 openid」，后者要提示下一步动作
                if name in self.adapter.known():
                    unresolved.append(name)
                else:
                    unknown.append(name)
                continue
            if target not in resolved:
                resolved.append(target)
        if unresolved:
            self.stats["rejected"] += 1
            return {
                "accepted": False,
                "error": (
                    f"群 {', '.join(unresolved)} 还没有 group_openid："
                    "请先把机器人拉进群，并在群里 @ 一下它，服务会自动获取（也可运行 qqpush-discover）"
                ),
                "pending_openid": unresolved,
                "known": self.adapter.known(),
            }
        if unknown:
            self.stats["rejected"] += 1
            return {
                "accepted": False,
                "error": f"未知的群：{', '.join(unknown)}",
                "known": self.adapter.known(),
            }
        if not resolved:
            self.stats["rejected"] += 1
            return {"accepted": False, "error": "没有解析出有效的推送目标"}

        if segments is None:
            segments = [{"type": "text", "data": {"text": str(message)}}]

        job = Job(job_id=uuid.uuid4().hex[:12], targets=resolved, segments=segments, source=source)
        for target in resolved:
            self.jobs.create(job.job_id, target, source)
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            self.stats["enqueue_timeout"] += 1
            LOG.error("队列已满(%d)，拒绝消息 %s", self.cfg.queue_maxsize, job.job_id)
            return {"accepted": False, "error": "服务繁忙，队列已满", "job_id": job.job_id}

        self.stats["accepted"] += 1
        LOG.info(
            "已入队 job=%s targets=%s chars=%d source=%s",
            job.job_id,
            resolved,
            len(to_plain_text(segments)),
            source,
        )
        return {
            "accepted": True,
            "job_id": job.job_id,
            "targets": resolved,
            "queued": self._queue.qsize(),
        }

    # ---------- worker ----------

    async def _worker(self, index: int) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._deliver(job)
            except asyncio.CancelledError:
                self._queue.task_done()
                raise
            except Exception:  # noqa: BLE001 - worker 不能因单条消息挂掉
                LOG.exception("投递 job=%s 时发生未预期异常", job.job_id)
            self._queue.task_done()

    async def _deliver(self, job: Job) -> list[SendResult]:
        results = await asyncio.gather(*(self._send_one(job, target) for target in job.targets))
        ok = sum(1 for r in results if r.ok)
        LOG.info(
            "job=%s 投递完成：成功 %d/%d %s",
            job.job_id,
            ok,
            len(results),
            "" if ok == len(results) else [r.to_dict() for r in results if not r.ok],
        )
        return list(results)

    async def _send_one(self, job: Job, target) -> SendResult:
        started = time.monotonic()
        attempts = self.cfg.onebot_retries + 1
        try:
            message_id = await asyncio.to_thread(self.adapter.send, target, job.segments)
        except OneBotError as exc:
            self.stats["failed"] += 1
            result = SendResult(
                target,
                False,
                error=str(exc),
                attempts=attempts,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            self._record_failure(job, target, str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜底，避免 worker 卡死
            self.stats["failed"] += 1
            LOG.exception("发送到 %s 时异常", target)
            result = SendResult(
                target,
                False,
                error=f"{type(exc).__name__}: {exc}",
                attempts=attempts,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            self._record_failure(job, target, result.error)
        else:
            self.stats["sent"] += 1
            result = SendResult(
                target,
                True,
                message_id=message_id,
                attempts=1,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        self.jobs.finish(job.job_id, target, result)
        return result

    def _record_failure(self, job: Job, target, error: str) -> None:
        """失败消息落盘，便于人工补发；写失败也不能影响主流程."""
        if not self.cfg.failed_log:
            return
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "job_id": job.job_id,
            "target": target,
            "channel": self.adapter.name,
            "source": job.source,
            "error": error,
            "message": job.segments,
        }
        try:
            with self._failed_lock, open(self.cfg.failed_log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + os.linesep)
        except OSError as exc:
            LOG.error("写失败日志出错：%s", exc)

    async def probe(self) -> dict:
        """探活：向当前通道要一次身份信息，确认凭据/连接可用."""
        client = self.adapter.client
        try:
            info = await asyncio.to_thread(client.login_info)
            key = "onebot" if self.adapter.name == "onebot" else "qqbot"
            return {key: "ok", "channel": self.adapter.name, "login": info}
        except OneBotError as exc:
            key = "onebot" if self.adapter.name == "onebot" else "qqbot"
            return {key: "error", "channel": self.adapter.name, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"channel": self.adapter.name, "error": f"{type(exc).__name__}: {exc}"}

    def queue_depth(self) -> int:
        return self._queue.qsize()
