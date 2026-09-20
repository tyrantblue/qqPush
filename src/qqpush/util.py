"""通用工具：日志、CQ 转义、markdown 降级、文本切分."""

from __future__ import annotations

import logging
import os
import re
import sys

LOG = logging.getLogger("qqpush")


def setup_logging(level: str = "INFO") -> None:
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(lvl)


def env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None else value.strip()


def env_int(name: str, default: int) -> int:
    raw = env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        LOG.warning("环境变量 %s=%r 不是整数，使用默认值 %s", name, raw, default)
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = env_str(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


def escape_cq(text: str) -> str:
    """转义 CQ 码特殊字符，避免文本被 OneBot 当作富文本指令解析.

    按 OneBot 11 规范顺序：先 & 再 [ ]，最后还原逗号。
    """
    return (
        text.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;").replace(",", "&#44;")
    )


_MD_CODE_BLOCK = re.compile(r"```[^\n`]*\n?(.*?)```", re.S)
_MD_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_MD_ITALIC = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$", re.M)
_MD_QUOTE = re.compile(r"^\s{0,3}>\s?", re.M)
_MD_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.M)
_MD_RULE = re.compile(r"^\s{0,3}(?:[-*_]\s*){3,}$", re.M)


def markdown_to_text(md: str) -> str:
    """把常见 markdown 语法降级成纯文本，QQ 不渲染 markdown."""
    text = _MD_CODE_BLOCK.sub(lambda m: m.group(1).strip("\n"), md)
    text = _MD_IMAGE.sub(lambda m: f"[图片:{m.group(1) or '无描述'}] {m.group(2)}", text)
    text = _MD_LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    text = _MD_INLINE_CODE.sub(lambda m: m.group(1), text)
    text = _MD_BOLD.sub(lambda m: m.group(1).strip(), text)
    text = _MD_ITALIC.sub(lambda m: m.group(1), text)
    text = _MD_HEADING.sub(lambda m: f"【{m.group(1)}】", text)
    text = _MD_QUOTE.sub("", text)
    text = _MD_BULLET.sub(lambda m: f"{m.group(1)}· ", text)
    text = _MD_RULE.sub("-" * 12, text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_text(text: str, limit: int) -> list[str]:
    """按长度切分文本，尽量在换行/句末断开（会去掉分片首尾空白）.

    用于失败日志等场景；消息段的切分见 message.split_segments（不丢空白）。
    """
    if limit <= 0 or len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = -1
        for sep in ("\n", "。", "！", "？", ". ", "!", "?", ";", "；", " "):
            pos = window.rfind(sep)
            if pos > cut:
                cut = pos + (len(sep) if sep != "\n" else 0)
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return [c for c in chunks if c] or [""]
