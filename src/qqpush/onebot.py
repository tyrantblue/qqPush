"""OneBot 11 HTTP 客户端（对接 NapCat / Lagrange / go-cqhttp）.

只依赖标准库 urllib，跑在线程池里（requests 少得可怜，无需引入 aiohttp）。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from .util import LOG


class OneBotError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, code: int = 0):
        super().__init__(message)
        self.retryable = retryable
        self.code = code


def _readable(detail: str) -> str:
    """错误响应往往是 JSON（中文被 \\u 转义），尽量取出人能看懂的信息."""
    detail = (detail or "").strip()
    if not detail:
        return ""
    try:
        data = json.loads(detail)
    except json.JSONDecodeError:
        return detail[:200]
    if isinstance(data, dict):
        for key in ("message", "wording", "msg", "error"):
            value = data.get(key)
            if value:
                return str(value)[:200]
    return detail[:200]


class SendResult:
    __slots__ = ("group_id", "ok", "message_id", "error", "attempts", "elapsed_ms")

    def __init__(
        self,
        group_id: int | str,
        ok: bool,
        message_id: int | str | None = None,
        error: str = "",
        attempts: int = 1,
        elapsed_ms: int = 0,
    ):
        self.group_id = group_id
        self.ok = ok
        self.message_id = message_id
        self.error = error
        self.attempts = attempts
        self.elapsed_ms = elapsed_ms

    def to_dict(self) -> dict:
        return {
            "group_id": self.group_id,
            "ok": self.ok,
            "message_id": self.message_id,
            "error": self.error,
            "attempts": self.attempts,
            "elapsed_ms": self.elapsed_ms,
        }


class OneBotClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        *,
        timeout: float = 10.0,
        retries: int = 2,
        rate_per_sec: float = 3.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retries = max(0, retries)
        self._min_interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._rate_lock = threading.Lock()
        self._next_slot = 0.0

    # ---------- 底层调用 ----------

    def _acquire_slot(self) -> None:
        """全局限速：QQ 对机器人调用频率敏感，超频会被风控."""
        if self._min_interval <= 0:
            return
        with self._rate_lock:
            now = time.monotonic()
            wait = self._next_slot - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_slot = max(now, self._next_slot) + self._min_interval

    def call(self, action: str, payload: dict) -> dict:
        """调用一个 OneBot action，返回 data 字段；失败抛 OneBotError."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "qqpush/1.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.base_url}/{action}", data=body, headers=headers, method="POST"
        )
        last_error: OneBotError | None = None
        for attempt in range(1, self.retries + 2):
            self._acquire_slot()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                detail = ""
                if exc.fp is not None:
                    try:
                        detail = exc.read().decode("utf-8", "replace")
                    except OSError:
                        detail = ""
                # 4xx 一般是 token/参数问题，重试没意义
                retryable = exc.code >= 500
                last_error = OneBotError(
                    f"HTTP {exc.code} {_readable(detail)}".strip(),
                    retryable=retryable,
                    code=exc.code,
                )
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = OneBotError(f"连接失败: {exc}", retryable=True)
            else:
                try:
                    data = json.loads(raw or "{}")
                except json.JSONDecodeError:
                    last_error = OneBotError(f"响应不是 JSON: {raw[:200]}", retryable=True)
                else:
                    if isinstance(data, dict) and data.get("status") == "failed":
                        retcode = data.get("retcode", -1)
                        msg = str(data.get("message") or data.get("wording") or "未知错误")
                        last_error = OneBotError(
                            f"OneBot 返回失败(retcode={retcode}): {msg}", retryable=True
                        )
                    else:
                        return data.get("data") if isinstance(data.get("data"), dict) else {}
            LOG.warning(
                "调用 %s 第 %d/%d 次失败：%s", action, attempt, self.retries + 1, last_error
            )
            if last_error is not None and not last_error.retryable:
                break
            if attempt <= self.retries:
                time.sleep(min(0.5 * attempt, 2.0))
        raise last_error or OneBotError("未知错误", retryable=True)

    # ---------- 业务方法 ----------

    def send_group_msg(self, group_id: int, segments: list[dict]) -> int | None:
        data = self.call("send_group_msg", {"group_id": group_id, "message": segments})
        message_id = data.get("message_id")
        try:
            return int(message_id) if message_id is not None else None
        except (TypeError, ValueError):
            return None

    def login_info(self) -> dict:
        return self.call("get_login_info", {})

    def status(self) -> dict:
        return self.call("get_status", {})
