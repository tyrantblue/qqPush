"""HTTP 入口：标准库 ThreadingHTTPServer + asyncio 队列.

入口线程只做鉴权/解析/入队，发送交给 asyncio worker，因此上游不会被 QQ
侧的延迟拖住；同时不引入任何运行时依赖。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import Config, GroupTable
from .core import PushService
from .message import BadRequest, build_segments, split_segments
from .util import LOG

MAX_BODY_BYTES = 1024 * 1024  # 1MB，足够放多条消息；防止超大请求打爆内存
START_TIME = time.time()

HELP = {
    "service": "qqpush",
    "endpoints": {
        "GET /healthz": "健康检查（含队列深度与当前通道）",
        "GET /groups": "列出可推送目标（官方通道含 group_openid），/openids 为别名",
        "POST /push": "推送消息，body: {message|messages, groups?, title?, source?, id?}",
        "GET /status/<job_id>": "查询某个任务的投递结果",
        "POST /reload": "重新加载 groups.json",
    },
    "auth": "请求头 X-Push-Token 或 Authorization: Bearer <QQPUSH_TOKEN>",
}


class PushHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "qqpush/1.0"
    protocol_version = "HTTP/1.1"

    # 由 build_server() 注入
    service: PushService
    cfg: Config
    groups: GroupTable
    loop: asyncio.AbstractEventLoop

    # ---------- 基础设施 ----------

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - 覆盖父类
        LOG.debug("HTTP %s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, payload: dict | list) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _fail(self, status: int, message: str, **extra) -> None:
        self._send(status, {"ok": False, "error": message, **extra})

    def _authorized(self) -> bool:
        token = self.cfg.push_token
        if not token:
            return True
        provided = self.headers.get("X-Push-Token", "")
        if not provided:
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                provided = auth[7:].strip()
        if not provided:
            query = parse_qs(urlparse(self.path).query)
            provided = (query.get("token") or [""])[0]
        return hmac.compare_digest(provided, token)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest("Content-Length 不合法") from None
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise BadRequest(f"请求体过大（>{MAX_BODY_BYTES} 字节）")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BadRequest(f"请求体不是合法 JSON: {exc}") from None
        if not isinstance(data, dict):
            raise BadRequest("请求体必须是 JSON 对象")
        return data

    def _run(self, coro):
        """在 HTTP 线程里同步等待事件循环中的协程结果."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=30)

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/", "/help"):
            self._send(200, {**HELP, "version": getattr(self.server, "qqpush_version", "1.0.0")})
        elif path in ("/healthz", "/health"):
            self._send(200, self._health())
        elif path in ("/groups", "/openids", "/targets"):
            if not self._authorized():
                self._fail(401, "鉴权失败：token 不正确")
                return
            self._send(200, {"ok": True, **self._targets_payload()})
        elif path.startswith("/status/"):
            if not self._authorized():
                self._fail(401, "鉴权失败：token 不正确")
                return
            job_id = path[len("/status/") :]
            result = self.service.jobs.get(job_id)
            if result is None:
                self._fail(404, f"没有找到任务 {job_id}")
            else:
                self._send(200, {"ok": True, **result})
        else:
            self._fail(404, "接口不存在")

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if not self._authorized():
            self._fail(401, "鉴权失败：token 不正确")
            return
        try:
            if path in ("/push", "/webhook", "/"):
                self._handle_push()
            elif path == "/reload":
                count = self.service.adapter.reload()
                self._send(
                    200,
                    {
                        "ok": True,
                        "channel": self.service.adapter.name,
                        "targets": count,
                        "names": self.service.adapter.known(),
                    },
                )
            else:
                self._fail(404, "接口不存在")
        except BadRequest as exc:
            self._fail(400, str(exc))
        except Exception as exc:  # noqa: BLE001 - 任何异常都回 500，不泄漏堆栈
            LOG.exception("处理 %s 出错", path)
            self._fail(500, f"服务内部错误: {type(exc).__name__}")

    # ---------- /push ----------

    def _handle_push(self) -> None:
        body = self._read_json()
        # 目标字段：groups / targets / group / openid / openids / to 都可，值为群名或 group_openid
        targets = None
        for key in ("groups", "targets", "group", "openid", "openids", "to"):
            value = body.get(key)
            if value:
                targets = value
                break
        if isinstance(targets, (str, int)):
            targets = [targets]
        if targets is not None and not isinstance(targets, list):
            raise BadRequest("groups/openid 必须是字符串或数组")

        cfg = self.cfg
        prefix = str(body.get("prefix") or cfg.default_prefix)
        title = str(body.get("title") or "")
        markdown = body.get("markdown")
        if markdown is None:
            markdown = cfg.convert_markdown

        raw_messages = body.get("messages", body.get("message"))
        if raw_messages is None:
            raise BadRequest("缺少字段 message 或 messages")
        is_batch = isinstance(raw_messages, list) and any(
            isinstance(x, (str, list)) for x in raw_messages
        )
        items = raw_messages if is_batch else [raw_messages]

        job_ids: list[str] = []
        resolved_targets: list[int] = []
        duplicates = 0
        rejected: list[dict] = []
        for index, item in enumerate(items):
            segments = build_segments(
                item,
                prefix=prefix,
                title=title,
                convert_markdown=bool(markdown),
                wrap_code_block=cfg.wrap_code_block,
            )
            dedup_key = str(body.get("id") or "")
            if dedup_key and len(items) > 1:
                dedup_key = f"{dedup_key}#{index}"
            for batch_index, batch in enumerate(split_segments(segments, cfg.group_msg_max_chars)):
                child_key = f"{dedup_key}:{batch_index}" if dedup_key else ""
                result = self._run(
                    self.service.submit(
                        None,
                        segments=batch,
                        targets=targets,
                        source=str(body.get("source") or self.client_source()),
                        dedup_key=child_key,
                    )
                )
                if result.get("accepted"):
                    job_ids.append(result["job_id"])
                    for group_id in result.get("targets", []):
                        if group_id not in resolved_targets:
                            resolved_targets.append(group_id)
                elif result.get("duplicate"):
                    duplicates += 1
                else:
                    rejected.append(result)

        if not job_ids:
            if duplicates and not rejected:
                self._send(200, {"ok": True, "duplicate": True, "message": "重复消息，已忽略"})
                return
            first = rejected[0] if rejected else {"error": "没有可投递的消息"}
            status = 400
            if first.get("pending_openid"):
                # 群配好了但还没拿到 group_openid，属于「还没准备好」而不是「找不到」
                status = 409
            elif "未知的群" in str(first.get("error", "")):
                status = 404
            elif "繁忙" in str(first.get("error", "")):
                status = 429
            extra = {"details": rejected[1:] or None}
            for key in ("known", "pending_openid"):
                if first.get(key):
                    extra[key] = first[key]
            self._fail(status, str(first.get("error")), **extra)
            return

        self._send(
            202,
            {
                "ok": True,
                "job_id": job_ids[0],
                "job_ids": job_ids,
                "targets": resolved_targets,
                **self._resolved_names(resolved_targets),
                "chunks": len(job_ids),
                "duplicates": duplicates,
                "rejected": rejected or None,
            },
        )

    def _resolved_names(self, resolved: list) -> dict:
        """把内部 ID 映射回群名，官方通道额外回一个 group_openid 字段方便调用方记录."""
        adapter = self.service.adapter
        name_by_target: dict = {}
        for name in adapter.known():
            target = adapter.resolve(name)
            if target is not None:
                name_by_target.setdefault(target, name)
        names = [name_by_target.get(t, "") for t in resolved]
        if adapter.name == "official":
            return {"names": names, "group_openid": resolved[0] if len(resolved) == 1 else None}
        return {"names": names}

    def client_source(self) -> str:
        return self.headers.get("X-Source") or self.address_string()

    def _targets_payload(self) -> dict:
        """目标清单：群名 -> 该通道发送时真正使用的 ID（官方通道就是 group_openid）."""
        adapter = self.service.adapter
        entries = []
        for name in adapter.known():
            target = adapter.resolve(name)
            entries.append({"name": name, "target": target})
        adapter_extra = adapter.describe()
        return {
            "channel": adapter.name,
            "target_hint": adapter.target_hint,
            "targets": adapter.known(),
            "openids": entries,
            "default_targets": self.groups.default_targets,
            **{k: v for k, v in adapter_extra.items() if k not in ("channel", "known_targets")},
        }

    # ---------- 健康检查 ----------

    def _health(self) -> dict:
        stats = dict(self.service.stats)
        stats["queue_depth"] = self.service.queue_depth()
        adapter = self.service.adapter
        return {
            "ok": True,
            "channel": adapter.name,
            "uptime_sec": int(time.time() - START_TIME),
            "targets": len(adapter.known()),
            "stats": stats,
        }


class HealthOnlyHandler(PushHTTPRequestHandler):
    """独立健康检查端口：只暴露 /healthz，不暴露 /push，方便容器探针."""

    def _health_path(self) -> bool:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/healthz", "/health"):
            self._send(200, self._health())
            return True
        if path in ("/", "/help"):
            self._send(
                200,
                {
                    "service": "qqpush",
                    "mode": "health-only",
                    "endpoints": {"GET /healthz": "健康检查"},
                },
            )
            return True
        return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._health_path():
            self._fail(404, "该端口只提供 /healthz")

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802
        self._fail(404, "该端口只提供 /healthz")


def build_server(
    cfg: Config, service: PushService, groups: GroupTable, loop: asyncio.AbstractEventLoop
):
    handler = type(
        "BoundPushHandler",
        (PushHTTPRequestHandler,),
        {"service": service, "cfg": cfg, "groups": groups, "loop": loop},
    )
    return ThreadingHTTPServer((cfg.host, cfg.port), handler)


def build_health_server(
    cfg: Config, service: PushService, groups: GroupTable, loop: asyncio.AbstractEventLoop
):
    handler = type(
        "BoundHealthHandler",
        (HealthOnlyHandler,),
        {"service": service, "cfg": cfg, "groups": groups, "loop": loop},
    )
    return ThreadingHTTPServer((cfg.host, cfg.health_port), handler)
