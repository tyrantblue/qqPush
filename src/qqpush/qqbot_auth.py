"""官方 QQ 机器人 access_token 管理.

AppID + AppSecret/ClientSecret -> access_token（默认 7200 秒有效）。
官方要求自行刷新，这里做线程安全缓存，并在剩余 <60 秒时提前换新。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from .util import LOG

TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
# 官方建议：上一个 token 过期前 60 秒内请求会拿到新 token
_REFRESH_MARGIN = 60.0


class BotAuthError(RuntimeError):
    def __init__(self, message: str, *, code: int = 0, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class TokenManager:
    """缓存 access_token，自动刷新；线程安全（发送线程与事件线程都会用到）."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        timeout: float = 10.0,
        token_url: str = TOKEN_URL,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.timeout = timeout
        self.token_url = token_url
        self._lock = threading.RLock()
        self._token = ""
        self._expires_at = 0.0
        self.refresh_count = 0

    def invalidate(self) -> None:
        """服务端返回鉴权失败时调用，强制下次重新获取."""
        with self._lock:
            self._token = ""
            self._expires_at = 0.0

    def get_token(self) -> str:
        with self._lock:
            now = time.time()
            if self._token and now < self._expires_at - _REFRESH_MARGIN:
                return self._token
        return self._fetch()

    def _fetch(self) -> str:
        payload = json.dumps({"appId": self.app_id, "clientSecret": self.app_secret}).encode(
            "utf-8"
        )
        request = urllib.request.Request(
            self.token_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "qqpush/1.1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
            raise BotAuthError(
                f"获取 access_token 失败：HTTP {exc.code} {detail[:200]}",
                code=exc.code,
                retryable=exc.code >= 500,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BotAuthError(f"获取 access_token 失败：连接错误 {exc}", retryable=True) from None

        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            raise BotAuthError(
                f"获取 access_token 返回非 JSON：{raw[:200]}", retryable=True
            ) from None

        # 注意：官方该接口失败时 HTTP 仍为 200，必须看 code
        code = int(data.get("code") or 0)
        token = data.get("access_token") or ""
        if code or not token:
            message = str(data.get("message") or data.get("msg") or "未知错误")
            hint = {
                100001: "请求过于频繁",
                100007: "AppID 无效或机器人状态异常（被封禁/已删除）",
                100016: "AppID 或 ClientSecret 不正确",
                10004: "机器人不存在",
            }.get(code, "")
            raise BotAuthError(
                f"获取 access_token 失败：code={code} {message}{'（' + hint + '）' if hint else ''}",
                code=code,
                # 频控可重试；凭据错误不可重试
                retryable=code == 100001,
            )

        try:
            expires_in = float(data.get("expires_in") or 7200)
        except (TypeError, ValueError):
            expires_in = 7200.0

        with self._lock:
            self._token = token
            self._expires_at = time.time() + expires_in
            self.refresh_count += 1
            LOG.debug(
                "已获取 access_token（第 %d 次），%.0f 秒后过期", self.refresh_count, expires_in
            )
        return token

    @property
    def expires_in(self) -> float:
        with self._lock:
            return max(0.0, self._expires_at - time.time())

    def auth_header(self) -> dict:
        """官方要求的请求头格式：Authorization: QQBot <access_token>."""
        return {"Authorization": f"QQBot {self.get_token()}"}
