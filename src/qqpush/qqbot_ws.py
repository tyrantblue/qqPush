"""官方 QQ 机器人事件监听（WebSocket 长连接）.

用途：官方接口发群消息需要 group_openid，而面板里看不到它，只能从事件里拿。
拿到后会按「群号 → 群名」自动写回 groups.json，之后就能用群名推送。

同时支持 `qqpush-discover` 命令行首次取号。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Callable

from .qqbot_auth import BotAuthError, TokenManager
from .util import LOG

# 事件订阅位：GROUP_AND_C2C_EVENT = 1<<25
INTENT_GROUP_AND_C2C = 1 << 25

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# 我们关心的事件
EVENT_GROUP_AT_MESSAGE = "GROUP_AT_MESSAGE_CREATE"
EVENT_GROUP_ADD_ROBOT = "GROUP_ADD_ROBOT"
EVENT_GROUP_DEL_ROBOT = "GROUP_DEL_ROBOT"
EVENT_GROUP_MSG_REJECT = "GROUP_MSG_REJECT"
EVENT_GROUP_MSG_RECEIVE = "GROUP_MSG_RECEIVE"
EVENT_READY = "READY"
EVENT_RESUMED = "RESUMED"

WS_ERROR_HINTS = {
    4001: "无效的 opcode",
    4002: "无效的 payload",
    4006: "session 失效，需要重新 identify",
    4007: "seq 错误",
    4008: "发送过快，需重连",
    4009: "连接过期，可 resume",
    4010: "无效的 shard",
    4011: "需要处理的 guild 过多",
    4012: "无效的 version",
    4013: "无效的 intent",
    4014: "intent 无权限（该事件类型未申请权限，鉴权会被关闭连接）",
    4900: "内部错误，请重连",
    4914: "机器人已下架，只允许连接沙箱环境",
    4915: "机器人已封禁",
}


class BotEventError(RuntimeError):
    pass


def fetch_gateway_url(
    token_manager: TokenManager, base_url: str = "https://api.bot.qq.com", timeout: float = 10.0
) -> str:
    """GET /gateway 拿到 wss 接入点."""
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/gateway",
        headers={**token_manager.auth_header(), "User-Agent": "qqpush/1.1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
        raise BotEventError(f"获取 WebSocket 接入点失败：HTTP {exc.code} {detail[:200]}") from None
    except BotAuthError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise BotEventError(f"获取 WebSocket 接入点失败：{exc}") from None
    url = str(data.get("url") or "")
    if not url:
        raise BotEventError(f"接入点响应异常：{str(data)[:200]}")
    return url


class GroupOpenIdRegistry:
    """group_openid -> 元信息，并负责把结果写回 groups.json."""

    def __init__(self, groups_file, on_change: Callable[[], None] | None = None):
        self.groups_file = groups_file
        self.on_change = on_change
        self._openids: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        self._openids = {}
        if not self.groups_file.exists():
            return
        try:
            raw = json.loads(self.groups_file.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, OSError) as exc:
            LOG.warning("读取群映射失败：%s", exc)
            return
        for name, info in (raw.get("group_openids") or {}).items():
            if isinstance(info, dict) and info.get("openid"):
                self._openids[str(name)] = dict(info)
            elif isinstance(info, str):  # 允许简写成 "群名": "openid"
                self._openids[str(name)] = {"openid": info}
        if self._openids:
            LOG.info("已加载 %d 个群 openid 映射：%s", len(self._openids), sorted(self._openids))

    def get(self, name: str) -> str | None:
        info = self._openids.get(str(name))
        return str(info["openid"]) if info else None

    def bound_openids(self) -> set[str]:
        return {str(v["openid"]) for v in self._openids.values() if v.get("openid")}

    def pending_names(self, configured: list[str]) -> list[str]:
        """已配置但还没有 openid 的群名（候选，用于排除法绑定）."""
        return [name for name in configured if self.get(name) is None]

    def by_group_number(self, group_number: str) -> tuple[str, str] | None:
        for name, info in self._openids.items():
            if str(info.get("group_number") or "") == str(group_number):
                return name, str(info["openid"])
        return None

    def known_numbers(self) -> dict[str, str]:
        return {
            str(v.get("group_number")): k for k, v in self._openids.items() if v.get("group_number")
        }

    def record(self, name: str, openid: str, group_number: str = "") -> bool:
        """记录一条映射；返回 True 表示有新内容需要落盘."""
        existing = self._openids.get(name)
        if (
            existing
            and existing.get("openid") == openid
            and str(existing.get("group_number") or "") == str(group_number)
        ):
            return False
        self._openids[name] = {"openid": openid, "group_number": str(group_number or "")}
        suffix = f"（群号 {group_number}）" if group_number else ""
        LOG.info("发现群 openid：%s -> %s%s", name, openid, suffix)
        self.save()
        return True

    def save(self) -> None:
        try:
            raw = json.loads(self.groups_file.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, OSError):
            raw = {}
        raw["group_openids"] = self._openids
        try:
            self.groups_file.parent.mkdir(parents=True, exist_ok=True)
            self.groups_file.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            if self.on_change:
                self.on_change()
        except OSError as exc:
            LOG.error("写回 %s 失败：%s", self.groups_file, exc)


class BotEventClient:
    """WebSocket 事件监听；断线自动 resume，并维护心跳."""

    def __init__(
        self,
        token_manager: TokenManager,
        *,
        intents: int = INTENT_GROUP_AND_C2C,
        base_url: str = "https://api.bot.qq.com",
        on_event: Callable[[str, dict], None] | None = None,
    ):
        self.tokens = token_manager
        self.intents = intents
        self.base_url = base_url
        self.on_event = on_event
        self.session_id = ""
        self.last_seq: int | None = None
        self.connected = False
        self.events_seen = 0

    async def start(self) -> None:
        """保持长连接直到被取消."""
        backoff = 1.0
        while True:
            try:
                await self._run_once()
                backoff = 1.0
            except asyncio.CancelledError:
                self.connected = False
                raise
            except BotEventError as exc:
                LOG.error("事件监听中断：%s，%.0fs 后重连", exc, backoff)
            except Exception as exc:  # noqa: BLE001 - 监听线程不能挂
                LOG.error("事件监听异常：%s: %s，%.0fs 后重连", type(exc).__name__, exc, backoff)
            self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _run_once(self) -> None:
        import websockets

        url = await asyncio.to_thread(fetch_gateway_url, self.tokens, self.base_url)
        LOG.info("连接官方事件网关 %s", url)
        async with websockets.connect(url, max_size=2**22) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if hello.get("op") != OP_HELLO:
                raise BotEventError(f"未收到 Hello：{str(hello)[:200]}")
            interval = float((hello.get("d") or {}).get("heartbeat_interval") or 45000) / 1000.0

            if self.session_id and self.last_seq is not None:
                await ws.send(
                    json.dumps(
                        {
                            "op": OP_RESUME,
                            "d": {
                                "token": f"QQBot {self.tokens.get_token()}",
                                "session_id": self.session_id,
                                "seq": self.last_seq,
                            },
                        }
                    )
                )
                LOG.info("尝试恢复会话 session=%s seq=%s", self.session_id, self.last_seq)
            else:
                await ws.send(
                    json.dumps(
                        {
                            "op": OP_IDENTIFY,
                            "d": {
                                "token": f"QQBot {self.tokens.get_token()}",
                                "intents": self.intents,
                                "shard": [0, 1],
                                "properties": {
                                    "$os": "linux",
                                    "$browser": "qqpush",
                                    "$device": "qqpush",
                                },
                            },
                        }
                    )
                )

            heartbeat = asyncio.create_task(self._heartbeat(ws, interval))
            try:
                async for raw in ws:
                    await self._handle(json.loads(raw))
            finally:
                heartbeat.cancel()

    async def _heartbeat(self, ws, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": self.last_seq}))
            except Exception:  # noqa: BLE001 - 连接已断，交给外层重连
                return

    async def _handle(self, payload: dict) -> None:
        op = payload.get("op")
        if payload.get("s") is not None:
            self.last_seq = payload["s"]
        if op == OP_HEARTBEAT_ACK:
            return
        if op == OP_HELLO:
            return
        if op == OP_RECONNECT:
            raise BotEventError("服务端要求重连")
        if op == OP_INVALID_SESSION:
            LOG.warning("会话失效，将重新 identify")
            self.session_id, self.last_seq = "", None
            raise BotEventError("会话失效")
        if op != OP_DISPATCH:
            LOG.debug("忽略 op=%s", op)
            return

        event = str(payload.get("t") or "")
        data = payload.get("d") or {}
        if event == EVENT_READY:
            self.session_id = str(data.get("session_id") or "")
            self.connected = True
            user = (data.get("user") or {}).get("username")
            LOG.info("事件通道就绪：机器人 %s，session=%s", user, self.session_id)
            return
        if event == EVENT_RESUMED:
            self.connected = True
            LOG.info("会话已恢复，事件补发完成")
            return

        self.events_seen += 1
        LOG.info("收到事件 %s%s", event, _event_hint(event, data))
        if self.on_event:
            try:
                self.on_event(event, data)
            except Exception:  # noqa: BLE001 - 事件处理失败不影响连接
                LOG.exception("处理事件 %s 出错", event)


def _event_hint(event: str, data: dict) -> str:
    """给日志补上关键标识，方便排查绑定问题."""
    openid = str(data.get("group_openid") or "")
    if not openid:
        return ""
    if event in (EVENT_GROUP_AT_MESSAGE, EVENT_GROUP_ADD_ROBOT):
        content = str(data.get("content") or "").strip()
        author = (data.get("author") or {}).get("username") or ""
        return f"（group_openid={openid}{f' 内容={content[:20]!r}' if content else ''}{f' 来自={author}' if author else ''}）"
    return f"（group_openid={openid}）"


def describe_event(event: str, data: dict) -> str:
    """把事件整理成一行可读信息，用于首次取号时的人工确认."""
    if event == EVENT_GROUP_AT_MESSAGE:
        author = (data.get("author") or {}).get("member_openid") or ""
        content = str(data.get("content") or "").strip()
        return (
            f"群消息：group_openid={data.get('group_openid')} "
            f"群号可能={data.get('group_id') or '未知'} 发送者={author} 内容={content[:40]!r}"
        )
    if event == EVENT_GROUP_ADD_ROBOT:
        return f"机器人被拉进群：group_openid={data.get('group_openid')} 操作者={(data.get('op_member_openid') or '')}"
    if event in (EVENT_GROUP_DEL_ROBOT,):
        return f"机器人被移出群：group_openid={data.get('group_openid')}"
    if event == EVENT_GROUP_MSG_REJECT:
        return f"群管理员关闭了机器人通知：group_openid={data.get('group_openid')}"
    if event == EVENT_GROUP_MSG_RECEIVE:
        return f"群管理员开启了机器人通知：group_openid={data.get('group_openid')}"
    return f"{event}: {json.dumps(data, ensure_ascii=False)[:200]}"
