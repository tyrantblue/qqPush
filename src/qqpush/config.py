"""配置与群映射表.

所有配置来自环境变量（见 .env.example）。群名称映射放在 data/groups.json：
  - channel=official：用 group_openids 段（名称 -> group_openid），可由事件监听自动写入
  - channel=onebot  ：用 groups 段（名称 -> 群号）
可通过 /reload 接口热更新，无需重启。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .util import LOG, env_bool, env_int, env_str

DEFAULT_GROUPS_FILE = "data/groups.json"
CHANNELS = ("official", "onebot")


@dataclass
class Config:
    # HTTP 监听
    host: str = "0.0.0.0"
    port: int = 8088
    health_port: int = 0  # >0 时额外开一个独立健康检查端口
    # 鉴权：外部调用 /push 时需要携带的共享密钥，留空表示不校验
    push_token: str = ""
    # 通道选择：official = 官方 QQ 机器人；onebot = NapCat/Lagrange
    channel: str = "official"
    # 官方 QQ 机器人侧
    qqbot_app_id: str = ""
    qqbot_app_secret: str = ""
    qqbot_api_base: str = "https://api.bot.qq.com"
    qqbot_intents: int = 1 << 25  # GROUP_AND_C2C_EVENT
    qqbot_listen: bool = True  # 是否启动 WebSocket 事件监听（用于取 group_openid）
    qqbot_max_chars: int = 1000  # 官方文本消息长度上限（保守值）
    qqbot_strip_urls: bool = False  # 官方可能拒绝含 URL 的消息，可开启自动省略
    qqbot_group_rate_per_minute: float = 18.0  # 官方限制每群 20 条/分钟
    # OneBot 11 侧
    onebot_base_url: str = "http://127.0.0.1:3000"
    onebot_token: str = ""
    onebot_timeout: float = 10.0
    onebot_retries: int = 2
    send_rate_limit_per_sec: float = 3.0
    group_msg_max_chars: int = 4500
    # 群映射
    groups_file: str = DEFAULT_GROUPS_FILE
    # 队列与并发
    queue_maxsize: int = 1000
    workers: int = 4
    dedup_ttl: float = 300.0
    # 行为
    default_prefix: str = ""
    convert_markdown: bool = True
    wrap_code_block: bool = True
    # 观测
    log_level: str = "INFO"
    failed_log: str = "data/failed.ndjson"

    @classmethod
    def from_env(cls) -> Config:
        cfg = cls(
            host=env_str("QQPUSH_HOST", "0.0.0.0"),
            port=env_int("QQPUSH_PORT", 8088),
            health_port=env_int("QQPUSH_HEALTH_PORT", 0),
            push_token=env_str("QQPUSH_TOKEN", ""),
            channel=env_str("QQPUSH_CHANNEL", "official").lower(),
            qqbot_app_id=env_str("QQBOT_APPID", ""),
            qqbot_app_secret=env_str("QQBOT_SECRET", ""),
            qqbot_api_base=env_str("QQBOT_API_BASE", "https://api.bot.qq.com").rstrip("/"),
            qqbot_intents=env_int("QQBOT_INTENTS", 1 << 25),
            qqbot_listen=env_bool("QQBOT_LISTEN", True),
            qqbot_max_chars=env_int("QQBOT_MAX_CHARS", 1000),
            qqbot_strip_urls=env_bool("QQBOT_STRIP_URLS", False),
            qqbot_group_rate_per_minute=float(env_int("QQBOT_GROUP_PER_MINUTE", 18)),
            onebot_base_url=env_str("ONEBOT_BASE_URL", "http://127.0.0.1:3000").rstrip("/"),
            onebot_token=env_str("ONEBOT_TOKEN", ""),
            onebot_timeout=float(env_int("ONEBOT_TIMEOUT", 10)),
            onebot_retries=env_int("ONEBOT_RETRIES", 2),
            send_rate_limit_per_sec=float(env_int("QQPUSH_SEND_RATE_PER_SEC", 3)),
            group_msg_max_chars=env_int("QQPUSH_MAX_CHARS", 4500),
            groups_file=env_str("QQPUSH_GROUPS_FILE", DEFAULT_GROUPS_FILE),
            queue_maxsize=env_int("QQPUSH_QUEUE_SIZE", 1000),
            workers=env_int("QQPUSH_WORKERS", 4),
            dedup_ttl=float(env_int("QQPUSH_DEDUP_TTL", 300)),
            default_prefix=env_str("QQPUSH_DEFAULT_PREFIX", ""),
            convert_markdown=env_bool("QQPUSH_MARKDOWN", True),
            wrap_code_block=env_bool("QQPUSH_CODE_BLOCK", True),
            log_level=env_str("QQPUSH_LOG_LEVEL", "INFO"),
            failed_log=env_str("QQPUSH_FAILED_LOG", "data/failed.ndjson"),
        )
        if not cfg.onebot_base_url:
            cfg.onebot_base_url = "http://127.0.0.1:3000"
        if cfg.channel not in CHANNELS:
            LOG.warning("未知通道 %r，回退到 official", cfg.channel)
            cfg.channel = "official"
        cfg.workers = max(1, min(cfg.workers, 64))
        cfg.onebot_retries = max(0, min(cfg.onebot_retries, 10))
        cfg.onebot_timeout = max(1.0, min(cfg.onebot_timeout, 120.0))
        cfg.queue_maxsize = max(1, cfg.queue_maxsize)
        cfg.qqbot_max_chars = max(1, cfg.qqbot_max_chars)
        cfg.qqbot_group_rate_per_minute = max(0.0, cfg.qqbot_group_rate_per_minute)
        return cfg

    @property
    def is_official(self) -> bool:
        return self.channel == "official"


@dataclass
class GroupTable:
    """群名称 -> 群号 映射，支持热更新."""

    path: Path
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _groups: dict[str, int] = field(default_factory=dict, repr=False)
    default_targets: list[str] = field(default_factory=list, repr=False)
    loaded_at: float = 0.0

    def load(self) -> int:
        """读取 groups.json；文件不存在时保持空表（不抛错，方便首启动）."""
        with self._lock:
            if not self.path.exists():
                LOG.warning("群映射文件不存在：%s（请复制 groups.example.json 后修改）", self.path)
                self._groups, self.default_targets, self.loaded_at = {}, [], time.time()
                return 0
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            groups: dict[str, int] = {}
            for name, gid in (raw.get("groups") or {}).items():
                try:
                    groups[str(name)] = int(gid)
                except (TypeError, ValueError):
                    LOG.warning("群映射 %s -> %r 不是合法群号，已跳过", name, gid)
            defaults = [str(x) for x in (raw.get("default_targets") or [])]
            self._groups, self.default_targets, self.loaded_at = groups, defaults, time.time()
            LOG.info("已加载 %d 个群映射，默认推送目标 %s", len(groups), defaults or "（无）")
            return len(groups)

    def resolve(self, target: str) -> int | None:
        """target 可以是配置里的名称，也可以直接是群号."""
        key = str(target).strip()
        if not key:
            return None
        with self._lock:
            if key in self._groups:
                return self._groups[key]
        if key.lstrip("-").isdigit():
            return int(key)
        return None

    @property
    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._groups)

    def name_by_number(self, number: str) -> str | None:
        """按群号反查配置里的群名（用于事件里自动认领 group_openid）."""
        wanted = str(number).strip()
        if not wanted:
            return None
        with self._lock:
            for name, group_id in self._groups.items():
                if str(group_id) == wanted:
                    return name
        return None

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._groups)
