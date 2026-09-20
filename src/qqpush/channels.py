"""通道适配层：让 /push 的语义在「官方 QQ 机器人」与「OneBot」之间保持一致.

上层（core.PushService / server）只依赖本模块的抽象：
  - 目标用「群名」表达，由适配器解析成该通道需要的 ID
    * official -> group_openid（字符串）
    * onebot   -> 群号（整数）
"""

from __future__ import annotations

from pathlib import Path

from .config import Config, GroupTable
from .qqbot_api import OfficialBotClient
from .qqbot_auth import TokenManager
from .qqbot_ws import GroupOpenIdRegistry
from .util import LOG


class ChannelAdapter:
    name = "base"
    target_hint = ""

    def resolve(self, target: str):
        raise NotImplementedError

    def send(self, target, segments: list[dict]):
        raise NotImplementedError

    def known(self) -> list[str]:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"channel": self.name, "known_targets": self.known()}

    def reload(self) -> int:
        """热加载映射文件（OneBot 群号表 / 官方 group_openid 表）."""
        raise NotImplementedError

    def close(self) -> None:
        return None


class OneBotAdapter(ChannelAdapter):
    name = "onebot"
    target_hint = "群名或群号"

    def __init__(self, groups: GroupTable, client):
        self.groups = groups
        self.client = client

    def resolve(self, target: str):
        return self.groups.resolve(target)

    def send(self, target, segments: list[dict]):
        return self.client.send_group_msg(int(target), segments)

    def known(self) -> list[str]:
        return self.groups.names

    def reload(self) -> int:
        return self.groups.load()


class OfficialAdapter(ChannelAdapter):
    name = "official"
    target_hint = "群名（或直接填 group_openid）"

    def __init__(
        self, client: OfficialBotClient, registry: GroupOpenIdRegistry, groups: GroupTable
    ):
        self.client = client
        self.registry = registry
        self.groups = groups

    def resolve(self, target: str):
        key = str(target).strip()
        if not key:
            return None
        openid = self.registry.get(key)
        if openid:
            return openid
        # 退化用法：直接给 openid
        if key.startswith("OPENID_") or (len(key) >= 24 and key.isalnum()):
            return key
        # 给的是 QQ 群号时，尝试通过已记录的群号反查
        found = self.registry.by_group_number(key)
        return found[1] if found else None

    def send(self, target, segments: list[dict]):
        return self.client.send_group_msg(str(target), segments)

    def known(self) -> list[str]:
        names = set(self.registry._openids) | set(self.groups.names)
        return sorted(names)

    def reload(self) -> int:
        self.groups.load()
        self.registry._load()
        return len(self.registry._openids)

    def describe(self) -> dict:
        return {
            "channel": self.name,
            "known_targets": self.known(),
            "app_id": self.client.app_id,
            "token_expires_in": int(self.client.tokens.expires_in),
        }


def build_adapter(
    cfg: Config,
    groups: GroupTable,
    *,
    on_registry_change=None,
) -> tuple[ChannelAdapter, OfficialBotClient | None, GroupOpenIdRegistry | None]:
    """按配置创建适配器；返回 (adapter, official_client, registry)."""
    if cfg.is_official:
        if not cfg.qqbot_app_id or not cfg.qqbot_app_secret:
            LOG.warning(
                "通道 official 但缺少 QQBOT_APPID / QQBOT_SECRET，发送会失败；"
                "请在 .env 中配置后重启"
            )
        tokens = TokenManager(cfg.qqbot_app_id, cfg.qqbot_app_secret, timeout=cfg.onebot_timeout)
        client = OfficialBotClient(
            cfg.qqbot_app_id,
            cfg.qqbot_app_secret,
            token_manager=tokens,
            base_url=cfg.qqbot_api_base,
            timeout=cfg.onebot_timeout,
            retries=cfg.onebot_retries,
            rate_per_sec=cfg.send_rate_limit_per_sec,
            group_rate_per_minute=cfg.qqbot_group_rate_per_minute,
            max_chars=cfg.qqbot_max_chars,
            strip_urls=cfg.qqbot_strip_urls,
        )
        registry = GroupOpenIdRegistry(
            Path(cfg.groups_file),
            on_change=on_registry_change or (lambda: groups.load()),
        )
        return OfficialAdapter(client, registry, groups), client, registry

    from .onebot import OneBotClient

    client = OneBotClient(
        cfg.onebot_base_url,
        cfg.onebot_token,
        timeout=cfg.onebot_timeout,
        retries=cfg.onebot_retries,
        rate_per_sec=cfg.send_rate_limit_per_sec,
    )
    return OneBotAdapter(groups, client), None, None
