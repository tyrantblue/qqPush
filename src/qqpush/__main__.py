"""程序入口：qqpush / python -m qqpush."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
import threading

from . import __version__
from .app import build_app
from .config import Config
from .server import build_health_server, build_server
from .util import LOG, setup_logging

HELP_TEXT = """qqpush —— 轻量的 QQ 群消息转发服务

用法：
  qqpush                # 用环境变量启动服务（推荐配合 .env 或 docker compose）
  qqpush --version      # 查看版本
  qqpush --help         # 查看帮助

两条通道（用 QQPUSH_CHANNEL 选择）：
  official  官方 QQ 机器人（默认）：配置 QQBOT_APPID / QQBOT_SECRET。
            发群消息需要 group_openid，本服务通过事件监听自动获取并写回 groups.json
  onebot    NapCat / Lagrange 等 OneBot 11 实现：配置 ONEBOT_BASE_URL

常用环境变量：
  QQPUSH_HOST / QQPUSH_PORT      监听地址与端口（默认 0.0.0.0:8088）
  QQPUSH_TOKEN                   调用 /push 的共享密钥（建议必填）
  QQPUSH_CHANNEL                 official | onebot（默认 official）
  QQBOT_APPID / QQBOT_SECRET     官方机器人凭据
  QQBOT_LISTEN                   是否监听事件以自动获取 group_openid（默认 true）
  ONEBOT_BASE_URL                OneBot HTTP 地址（channel=onebot 时使用）
  QQPUSH_GROUPS_FILE             群映射文件（默认 data/groups.json）
完整说明见 README.md
"""


WEAK_TOKENS = {"change-me-please", "changeme", "test", "test-token", "123456", "password"}


def _check_push_token(cfg: Config) -> None:
    """启动时检查推送密钥强度：公网可访问又用弱口令，等于把发消息权限交出去."""
    token = cfg.push_token
    if not token:
        LOG.warning(
            "未设置 QQPUSH_TOKEN：任何能访问 %s:%s 的人都可以向你的群发消息。"
            "公网部署请务必设置（例如 openssl rand -hex 16）",
            cfg.host,
            cfg.port,
        )
        return
    if token.strip().lower() in WEAK_TOKENS or len(token) < 12:
        LOG.warning(
            "QQPUSH_TOKEN 过弱（示例值或长度 <12）：建议改成随机串，"
            "例如 `openssl rand -hex 16`，然后重启服务"
        )


async def run(cfg: Config) -> None:
    app = build_app(cfg)
    groups = app.groups
    service = app.service
    loop = asyncio.get_running_loop()

    servers = [build_server(cfg, service, groups, loop)]
    if cfg.health_port:
        servers.append(build_health_server(cfg, service, groups, loop))

    for srv in servers:
        host, port = srv.server_address[0], srv.server_address[1]
        LOG.info("HTTP 监听 http://%s:%s", host, port)

    _check_push_token(cfg)

    if cfg.is_official:
        LOG.info(
            "通道 official：AppID=%s，API=%s", cfg.qqbot_app_id or "（未配置）", cfg.qqbot_api_base
        )
        if app.registry:
            LOG.info(
                "已记录的群 openid：%s", sorted(app.registry._openids) or "（无，等事件自动获取）"
            )
        if not cfg.qqbot_app_id or not cfg.qqbot_app_secret:
            LOG.warning("缺少 QQBOT_APPID / QQBOT_SECRET，无法获取 access_token，推送会失败")
    else:
        LOG.info("通道 onebot：目标 %s", cfg.onebot_base_url)
    LOG.info(
        "群映射 %s -> %s，默认目标 %s",
        cfg.groups_file,
        groups.snapshot(),
        groups.default_targets or "（无）",
    )

    await service.start()

    if app.event_client is not None:
        app.background.append(asyncio.create_task(app.event_client.start(), name="qqpush-events"))
        LOG.info("事件监听已启动（intents=%s），用于自动获取 group_openid", cfg.qqbot_intents)

    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop_event.set)

    threads = [
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
        for srv in servers
    ]
    for thread in threads:
        thread.start()

    LOG.info("qqpush %s 启动完成：GET /healthz、POST /push", __version__)
    with contextlib.suppress(asyncio.CancelledError):
        await stop_event.wait()
    LOG.info("收到退出信号，开始优雅关闭…")

    for srv in servers:
        srv.shutdown()
        srv.server_close()
    for task in app.background:
        task.cancel()
    if app.background:
        await asyncio.gather(*app.background, return_exceptions=True)
    await service.stop()
    for thread in threads:
        thread.join(timeout=3)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-V", "--version", "version"):
        print(f"qqpush {__version__}")
        return 0
    if argv and argv[0] in ("-h", "--help", "help"):
        print(HELP_TEXT)
        return 0
    cfg = Config.from_env()
    setup_logging(cfg.log_level)
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        LOG.info("已中断")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
