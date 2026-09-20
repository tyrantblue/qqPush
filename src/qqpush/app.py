"""服务装配：配置 -> 群映射 -> 通道适配器 -> 推送服务.

把装配逻辑独立出来，`qqpush`（常驻服务）与 `qqpush-discover`（首次取 group_openid）共用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .channels import ChannelAdapter, build_adapter
from .config import Config, GroupTable
from .core import PushService
from .qqbot_api import OfficialBotClient
from .qqbot_ws import (
    EVENT_GROUP_ADD_ROBOT,
    EVENT_GROUP_AT_MESSAGE,
    BotEventClient,
    GroupOpenIdRegistry,
)
from .util import LOG


@dataclass
class App:
    cfg: Config
    groups: GroupTable
    adapter: ChannelAdapter
    service: PushService
    bot: OfficialBotClient | None = None
    registry: GroupOpenIdRegistry | None = None
    event_client: BotEventClient | None = None
    background: list = field(default_factory=list)


def auto_record_group(
    registry: GroupOpenIdRegistry, groups: GroupTable, event: str, data: dict
) -> bool:
    """把事件里的 group_openid 绑定到配置里的群名，并写回 groups.json.

    官方事件里的 `group_id` 就是 group_openid（不是真实群号），所以无法按群号匹配，
    这里用「排除法」：
      1. 事件自身带的可读群号（部分实现才有）优先；
      2. 否则，如果只剩一个「已配置但没有 openid」的群，就认定是它；
      3. 候选不唯一时不猜，打印事件内容让人工绑定，避免绑错群推错消息。
    """
    openid = str(data.get("group_openid") or "")
    if not openid:
        return False
    if event not in (EVENT_GROUP_AT_MESSAGE, EVENT_GROUP_ADD_ROBOT):
        return False
    # 已经绑定过的群，事件直接忽略
    if openid in registry.bound_openids():
        return False

    # 1) 事件里若有真正可读的群号（与 openid 不同），先按群号匹配
    number = str(data.get("group_id") or "")
    if number and number != openid:
        matched = groups.name_by_number(number) or ""
        if not matched:
            found = registry.by_group_number(number)
            matched = found[0] if found else ""
        if matched:
            return registry.record(matched, openid, number)

    # 2) 排除法：只剩一个待绑定群时直接绑
    configured = groups.names
    pending = registry.pending_names(configured)
    if len(pending) == 1:
        LOG.info("事件 %s 带来新的 group_openid，唯一待绑定群「%s」自动认领", event, pending[0])
        return registry.record(pending[0], openid, number if number and number != openid else "")
    if not configured:
        LOG.warning(
            "收到 %s 事件，group_openid=%s，但 groups.json 还没配置任何群。"
            "请先在 groups 里加上群名和群号，重启后重新 @ 一次机器人",
            event,
            openid,
        )
        return False

    # 3) 候选不唯一：只提示，不猜
    LOG.warning(
        "收到 %s 事件，group_openid=%s，但还有多个群待绑定 %s，无法确定是哪一个。"
        '请在 groups.json 的 group_openids 里手动写："群名": {"openid": "%s"}',
        event,
        openid,
        pending,
        openid,
    )
    return False


def build_app(cfg: Config) -> App:
    groups = GroupTable(path=Path(cfg.groups_file))
    groups.load()
    adapter, bot, registry = build_adapter(cfg, groups)
    service = PushService(cfg, groups, adapter)
    app = App(cfg=cfg, groups=groups, adapter=adapter, service=service, bot=bot, registry=registry)

    if cfg.is_official and registry is not None and cfg.qqbot_listen and bot is not None:

        def on_event(event: str, data: dict) -> None:
            auto_record_group(registry, groups, event, data)

        app.event_client = BotEventClient(
            bot.tokens, intents=cfg.qqbot_intents, base_url=cfg.qqbot_api_base, on_event=on_event
        )

    return app
