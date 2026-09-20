"""qqpush-discover：首次取 group_openid 的小工具.

官方接口发群消息需要 group_openid，而开放平台面板里看不到它，只能从事件里拿。
用法：
  1) 在群里 @ 一下机器人（或把机器人拉进群）
  2) 运行 qqpush-discover，看到目标群后 Ctrl+C 退出
  3) 如果 groups.json 里配了对应群号，会自动写回 group_openids；否则手工复制
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

from .app import auto_record_group
from .config import Config
from .qqbot_api import BotApiError, OfficialBotClient
from .qqbot_auth import TokenManager
from .qqbot_ws import BotEventClient, GroupOpenIdRegistry, describe_event, fetch_gateway_url
from .util import LOG, setup_logging

HELP_TEXT = """qqpush-discover —— 获取群 group_openid

前置：
  1) .env 里配置 QQBOT_APPID / QQBOT_SECRET
  2) 把机器人拉进目标群（未上线时用开放平台的沙箱群）
  3) groups.json 配好 groups（群名 -> 群号），事件到达时会自动写回 group_openids
运行后在群里 @ 一下机器人，终端会打印 group_openid。
"""


async def _reply_test(cfg: Config, client: OfficialBotClient) -> int:
    """自检：你在群里 @ 机器人后，立刻用事件的 msg_id 做一次被动回复.

    被动回复是基础能力；如果它成功而主动推送报 40034105，
    说明只是该群的「主动消息」权限/开关问题，而不是机器人不能用。
    """
    state: dict = {}

    def on_event(event: str, data: dict) -> None:
        print(f"  → {event}: {describe_event(event, data)}", flush=True)
        if event != "GROUP_AT_MESSAGE_CREATE" or state.get("replied"):
            return
        openid = str(data.get("group_openid") or "")
        msg_id = str(data.get("id") or "")
        if not openid or not msg_id:
            print("     ⚠️ 事件缺少 group_openid 或 id，无法被动回复", flush=True)
            return
        state["replied"] = True
        try:
            sent = client.send_passive_reply(
                openid, msg_id, "qqpush 被动回复自检成功 ✅（收到这条说明机器人可以在本群发言）"
            )
            print(f"     ✅ 被动回复成功，message_id={sent}", flush=True)
            print(
                "     结论：机器人可以发言。若主动推送仍报 40034105，"
                "就是该群的「主动消息」权限/开关问题（群管理员可在机器人资料页开启通知）",
                flush=True,
            )
        except BotApiError as exc:
            print(f"     ❌ 被动回复失败：{exc}", flush=True)
            print(
                "     说明机器人在本群发言被拒，常见原因：机器人未上线、沙箱群未配置、"
                "或该群禁止机器人发言",
                flush=True,
            )
        state["done"] = True

    listener = BotEventClient(
        client.tokens, intents=cfg.qqbot_intents, base_url=cfg.qqbot_api_base, on_event=on_event
    )
    print("请在群里 @ 一下机器人（我会立刻用 msg_id 回一条）…\n", flush=True)
    task = asyncio.create_task(listener.start())
    try:
        while not state.get("done"):
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        pass
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return 0 if state.get("done") else 1


async def _discover(cfg: Config, duration: float | None) -> int:
    if not cfg.qqbot_app_id or not cfg.qqbot_app_secret:
        LOG.error("请先在 .env 或环境变量里配置 QQBOT_APPID / QQBOT_SECRET")
        return 2

    tokens = TokenManager(cfg.qqbot_app_id, cfg.qqbot_app_secret)
    client = OfficialBotClient(cfg.qqbot_app_id, cfg.qqbot_app_secret, token_manager=tokens)
    try:
        info = await asyncio.to_thread(client.login_info)
        LOG.info("凭据有效：机器人 %s（id=%s）", info.get("nickname"), info.get("id"))
    except Exception as exc:  # noqa: BLE001
        LOG.error("凭据校验失败：%s", exc)
        return 2

    registry = GroupOpenIdRegistry(cfg.groups_file)
    from .config import GroupTable

    groups = GroupTable(path=cfg.groups_file)
    groups.load()
    if groups.names:
        LOG.info("groups.json 已配置群：%s", groups.names)
    else:
        LOG.warning("groups.json 还没有配置群号，将只打印事件内容，不自动写回")

    gateway = await asyncio.to_thread(fetch_gateway_url, tokens, cfg.qqbot_api_base)
    LOG.info("网关地址：%s", gateway)

    def on_event(event: str, data: dict) -> None:
        print(f"  → {event}: {describe_event(event, data)}", flush=True)
        if event in ("GROUP_AT_MESSAGE_CREATE", "GROUP_ADD_ROBOT"):
            openid = str(data.get("group_openid") or "")
            if not auto_record_group(registry, groups, event, data) and openid:
                print(
                    "     提示：把这个 openid 填到 groups.json 的 group_openids 里即可开始推送\n"
                    f"     group_openid = {openid}",
                    flush=True,
                )

    listener = BotEventClient(
        tokens, intents=cfg.qqbot_intents, base_url=cfg.qqbot_api_base, on_event=on_event
    )
    print("正在监听事件（在群里 @ 一下机器人）… 按 Ctrl+C 退出\n", flush=True)
    task = asyncio.create_task(listener.start())
    try:
        if duration:
            await asyncio.wait_for(task, timeout=duration)
        else:
            await task
    except TimeoutError:
        pass
    except asyncio.CancelledError:
        pass
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    if registry._openids:
        print("\n已记录的映射：", flush=True)
        for name, info in registry._openids.items():
            print(f"  {name or '(未命名)'}: {info}", flush=True)
    print(f"\n共收到 {listener.events_seen} 个事件。", flush=True)
    return 0


async def _run(cfg: Config, duration: float | None, reply_test: bool) -> int:
    if not cfg.qqbot_app_id or not cfg.qqbot_app_secret:
        LOG.error("请先在 .env 或环境变量里配置 QQBOT_APPID / QQBOT_SECRET")
        return 2
    if reply_test:
        client = OfficialBotClient(cfg.qqbot_app_id, cfg.qqbot_app_secret)
        try:
            info = await asyncio.to_thread(client.login_info)
        except BotApiError as exc:
            LOG.error("凭据校验失败：%s", exc)
            return 2
        LOG.info("凭据有效：机器人 %s", info.get("nickname"))
        return await _reply_test(cfg, client)
    return await _discover(cfg, duration)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help", "help"):
        print(HELP_TEXT)
        print("  qqpush-discover --reply-test   被动回复自检（判断机器人能否在本群发言）")
        return 0
    reply_test = "--reply-test" in argv
    duration = None
    if argv and argv[0] in ("-t", "--timeout") and len(argv) > 1:
        duration = float(argv[1])
    cfg = Config.from_env()
    setup_logging(cfg.log_level)
    try:
        return asyncio.run(_run(cfg, duration, reply_test))
    except KeyboardInterrupt:
        print("\n已退出", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
