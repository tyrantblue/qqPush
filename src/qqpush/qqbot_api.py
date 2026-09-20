"""官方 QQ 机器人 OpenAPI 客户端（api.bot.qq.com）.

只做「主动发送群消息」这条主链路：/v2/groups/{group_openid}/messages。
需要事先拿到 group_openid（见 qqbot_ws.py 的事件监听）。

官方错误约定：失败时 HTTP 可能仍是 200，必须看响应体里的 code。
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request

from .message import segments_to_text
from .onebot import OneBotError
from .qqbot_auth import BotAuthError, TokenManager
from .util import LOG

API_BASE = "https://api.bot.qq.com"

# 出错后值得重试的错误码（频控 / 服务端抖动 / 富媒体转存失败）
RETRYABLE_CODES = {
    100001,
    11281,  # 检查管理员失败，系统错误
    11282,  # 检查管理员未通过（按官方说明重试一次）
    304061,
    40034004,
    40034100,
    50055001,
    50055006,
    1100100,  # 安全打击：消息被限频
    1100300,  # 系统内部错误
}
# 明确不该重试的错误码 -> 人话解释
FATAL_CODES = {
    22006: "消息类型与内容不匹配",
    304036: "无 Markdown 模板权限（个人机器人默认没有，请用纯文本）",
    304103: "被动回复的消息 ID 已过期",
    340069: "消息类型无效",
    40034006: "消息内容违规",
    40034024: "msg_id 无效或越权",
    40034029: "内嵌键盘行列超限",
    40034101: "机器人非群成员：请先把机器人拉进群",
    40034105: "主动消息无权限：群管理员可能关闭了机器人通知，或该群不支持主动消息",
    40034127: "无 Markdown 模板权限",
    40034128: "被动回复时间或次数超限",
    40054002: "机器人被禁言",
    40054003: "机器人不是群成员：请先把机器人拉进群",
    40054010: "不允许发送 URL：请开启 QQBOT_STRIP_URLS 或去掉链接",
    40054016: "机器人已下线（需在开放平台上线，或只能使用沙箱群）",
    # 公共错误码
    11241: "请求缺少 access_token",
    11243: "access_token 校验未通过，请检查 AppID/AppSecret",
    11251: "AppID/token 无法识别",
    11255: "请求的资源不存在：group_openid 不对，或机器人已不在该群（重新获取 openid）",
    11253: "该机器人应用没有调用此接口的权限，需要向平台申请",
    11254: "该接口对当前机器人被封禁",
    11265: "机器人已被封禁",
    50006: "消息为空",
    50035: "form-data 内容异常",
    10004: "频道不存在或机器人未加入",
    # 发消息 / 安全打击（1xxxxxx）
    1100101: "内容涉嫌敏感，被安全拦截",
    1100102: "暂未获得该功能体验资格",
    1100103: "被安全打击",
    1100104: "该群已失效或当前群不存在",
    1100301: "调用方不是群成员",
    1100308: "触发频道内限频",
}
# 长退避：频控类错误等一下更有意义
BACKOFF_LONG = (5.0, 10.0, 20.0)

_URL_PATTERN = re.compile(r"https?://\S+|www\.\S+", re.I)


# 频控/需调整类错误的人话解释（这些是会重试的）
RETRYABLE_HINTS = {
    100001: "接口请求过于频繁",
    11281: "官方侧系统错误（检查管理员失败）",
    11282: "官方侧系统错误（检查管理员未通过）",
    304061: "消息内容无效",
    40034004: "富媒体转存失败",
    40034100: "主动消息超过频控（每群 20 条/分钟、每天 1000 条）",
    50055001: "官方侧发送异常",
    50055006: "官方侧 ARK 消息异常",
    1100100: "安全打击：消息被限频",
    1100300: "官方侧系统内部错误",
}


def describe_code(code: int) -> str:
    return FATAL_CODES.get(code) or RETRYABLE_HINTS.get(code) or "未知错误"


class BotApiError(OneBotError):
    """官方接口错误（沿用 OneBotError 便于上层统一处理）."""

    def __init__(
        self, message: str, *, code: int = 0, retryable: bool = False, http_status: int = 0
    ):
        super().__init__(message, retryable=retryable, code=code)
        self.http_status = http_status


class RateLimiter:
    """两档限速：全局 QPS + 每群每分钟（对应官方 100 QPS / 20 qpm 的限制）."""

    def __init__(self, per_second: float = 5.0, per_group_per_minute: float = 18.0):
        self._min_interval = 1.0 / per_second if per_second > 0 else 0.0
        self._window = 60.0
        self._per_group_limit = per_group_per_minute
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self._group_hits: dict[str, list[float]] = {}

    def wait(self, group_openid: str) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                if self._per_group_limit > 0 and group_openid:
                    hits = [
                        t for t in self._group_hits.get(group_openid, []) if now - t < self._window
                    ]
                    self._group_hits[group_openid] = hits
                    group_wait = (
                        self._window - (now - hits[0])
                        if len(hits) >= self._per_group_limit
                        else 0.0
                    )
                else:
                    group_wait = 0.0
                global_wait = max(0.0, self._next_slot - now)
                wait = max(group_wait, global_wait)
                if wait <= 0:
                    self._next_slot = max(now, self._next_slot) + self._min_interval
                    if group_openid and self._per_group_limit > 0:
                        self._group_hits.setdefault(group_openid, []).append(now)
                    return
            time.sleep(min(wait, 5.0) + 0.01)


class OfficialBotClient:
    """官方机器人客户端：send_group_msg(openid, segments) -> message_id."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        token_manager: TokenManager | None = None,
        base_url: str = API_BASE,
        timeout: float = 10.0,
        retries: int = 2,
        rate_per_sec: float = 5.0,
        group_rate_per_minute: float = 18.0,
        max_chars: int = 1000,
        strip_urls: bool = False,
        msg_seq_start: int = 1,
    ):
        self.app_id = app_id
        self.tokens = token_manager or TokenManager(
            app_id,
            app_secret,
            timeout=timeout,
            token_url=f"{base_url.rstrip('/')}/app/getAppAccessToken",
        )
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = max(0, retries)
        self.max_chars = max_chars
        self.strip_urls = strip_urls
        self.limiter = RateLimiter(rate_per_sec, group_rate_per_minute)
        self._seq_lock = threading.Lock()
        self._next_seq: dict[str, int] = {}
        self._msg_seq_start = msg_seq_start

    # ---------- 底层 ----------

    def _next_msg_seq(self, group_openid: str) -> int:
        """官方要求：相同 msg_id+msg_seq 会去重，主动消息用自增值避免被判重."""
        with self._seq_lock:
            value = self._next_seq.get(group_openid, self._msg_seq_start)
            self._next_seq[group_openid] = value + 1
            return value

    def _request(
        self, method: str, path: str, payload: dict | None = None, *, auth: bool = True
    ) -> dict:
        """统一的官方接口调用：处理 HTTP 错误与响应体里的 code."""
        body = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        )
        headers = {"User-Agent": "qqpush/1.1"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            headers.update(self.tokens.auth_header())
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=body, headers=headers, method=method
        )
        trace_id = ""
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                status = response.status
                trace_id = response.headers.get("X-Tps-trace-ID", "") or ""
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
            trace_id = (exc.headers.get("X-Tps-trace-ID", "") if exc.headers else "") or ""
            raise self._classify(detail, http_status=exc.code, trace_id=trace_id) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BotApiError(f"连接官方接口失败: {exc}", retryable=True) from None

        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise BotApiError(
                f"官方接口返回非 JSON（HTTP {status}）: {raw[:200]}", retryable=True
            ) from None
        if not isinstance(data, dict):
            raise BotApiError(f"官方接口返回异常结构: {str(data)[:200]}", retryable=True) from None
        code = int(data.get("code") or data.get("err_code") or 0)
        if code:
            raise self._classify(
                json.dumps(data, ensure_ascii=False), http_status=status, trace_id=trace_id
            ) from None
        return data

    def _post(self, path: str, payload: dict, *, auth: bool = True) -> dict:
        return self._request("POST", path, payload, auth=auth)

    @staticmethod
    def _classify(detail: str, *, http_status: int = 0, trace_id: str = "") -> BotApiError:
        code = 0
        message = ""
        try:
            data = json.loads(detail)
            if isinstance(data, dict):
                code = int(data.get("code") or data.get("err_code") or 0)
                message = str(data.get("message") or data.get("msg") or data.get("err_msg") or "")
                trace_id = str(data.get("trace_id") or trace_id or "")
        except (json.JSONDecodeError, TypeError, ValueError):
            message = detail[:200]
        hint = describe_code(code) if code else ""
        text = f"官方接口返回错误 code={code} {message}".strip()
        if hint:
            text += f"（{hint}）"
        if trace_id:
            # 官方文档建议：找平台协助时提供 trace_id
            text += f" [trace_id={trace_id}]"
        retryable = code in RETRYABLE_CODES or http_status >= 500 or (not code and http_status == 0)
        return BotApiError(text, code=code, retryable=retryable, http_status=http_status)

    # ---------- 业务 ----------

    def prepare_content(self, segments: list[dict]) -> str:
        text = segments_to_text(segments)
        if self.strip_urls:
            text = _URL_PATTERN.sub("（链接已省略）", text)
        if len(text) > self.max_chars:
            text = text[: self.max_chars - 1] + "…"
        return text

    def send_group_msg(self, group_openid: str, segments: list[dict]) -> str | None:
        """向群发送文本消息（主动消息，无需 msg_id）."""
        content = self.prepare_content(segments)
        if not content:
            raise BotApiError("消息内容为空，官方通道只能发文本", code=22006)
        payload = {"msg_type": 0, "content": content, "msg_seq": self._next_msg_seq(group_openid)}
        last_error: BotApiError | None = None
        for attempt in range(1, self.retries + 2):
            self.limiter.wait(group_openid)
            try:
                data = self._post(f"/v2/groups/{group_openid}/messages", payload)
                return str(data.get("id") or "") or None
            except BotAuthError as exc:
                last_error = BotApiError(str(exc), code=exc.code, retryable=exc.retryable)
                self.tokens.invalidate()
            except BotApiError as exc:
                last_error = exc
                if exc.code in (11244, 11253, 40034024):  # 鉴权/越权类，换 token 再试
                    self.tokens.invalidate()
            if last_error is not None and not last_error.retryable:
                break
            if attempt <= self.retries:
                if last_error is not None and last_error.code in RETRYABLE_CODES:
                    delay = BACKOFF_LONG[min(attempt - 1, len(BACKOFF_LONG) - 1)]
                else:
                    delay = min(0.5 * attempt, 3.0)
                LOG.warning(
                    "发送群消息第 %d/%d 次失败：%s，%.1fs 后重试",
                    attempt,
                    self.retries + 1,
                    last_error,
                    delay,
                )
                time.sleep(delay)
                continue
            break
        raise last_error or BotApiError("发送失败", retryable=True)

    def send_passive_reply(
        self, group_openid: str, msg_id: str, content: str, *, msg_seq: int = 1
    ) -> str | None:
        """被动回复：必须带 msg_id（来自事件 d.id），5 分钟内有效、每条最多回 5 次.

        用来判断「机器人到底能不能在该群发言」：主动消息可能需要额外权限/开关，
        被动回复则是基础能力。
        """
        if not content or not content.strip():
            raise BotApiError("消息内容为空", code=22006)
        data = self._post(
            f"/v2/groups/{group_openid}/messages",
            {"msg_type": 0, "content": content, "msg_id": msg_id, "msg_seq": msg_seq},
        )
        return str(data.get("id") or "") or None

    # 与 OneBot 客户端保持同一接口，便于上层无感切换
    def login_info(self) -> dict:
        data = self._request("GET", "/users/@me")
        return {"id": data.get("id"), "nickname": data.get("username"), "raw": data}

    def status(self) -> dict:
        return {"app_id": self.app_id, "token_expires_in": int(self.tokens.expires_in)}
