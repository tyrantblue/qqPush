"""消息体构造：文本 / OneBot 消息段 / markdown 降级."""

from __future__ import annotations

from typing import Any

from .util import escape_cq, markdown_to_text

# OneBot 11 消息段类型白名单，避免外部请求把任意结构透传给机器人
ALLOWED_SEGMENTS = {"text", "image", "face", "at", "record", "video", "reply", "json", "music"}


class BadRequest(ValueError):
    """请求体不合法（调用方的问题，返回 400）."""


def normalize_message(raw: Any, *, convert_markdown: bool = True) -> list[dict]:
    """把外部传入的 message 规整成 OneBot 消息段数组.

    支持两种形态：
      - 字符串：按纯文本处理，CQ 码特殊字符会被转义
      - 数组：OneBot 消息段（[{"type": "text", "data": {"text": "hi"}}]）
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise BadRequest("message 不能为空")
        if convert_markdown:
            text = markdown_to_text(text)
        return text_to_segments(text)
    if isinstance(raw, list):
        if not raw:
            raise BadRequest("message 数组不能为空")
        segments: list[dict] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise BadRequest(f"message[{index}] 必须是对象")
            seg_type = str(item.get("type") or "").strip()
            data = item.get("data")
            if seg_type not in ALLOWED_SEGMENTS:
                raise BadRequest(f"不支持的消息段类型：{seg_type!r}")
            if not isinstance(data, dict):
                data = {}
            if seg_type == "text":
                segments.append({"type": "text", "data": {"text": str(data.get("text", ""))}})
            else:
                payload = {str(k): str(v) for k, v in data.items() if isinstance(k, str)}
                segments.append({"type": seg_type, "data": payload})
        if not segments:
            raise BadRequest("message 数组不能为空")
        return segments
    raise BadRequest("message 必须是字符串或 OneBot 消息段数组")


def text_to_segments(text: str) -> list[dict]:
    return [{"type": "text", "data": {"text": escape_cq(text)}}]


def _seg_len(seg: dict) -> int:
    return len(str(seg.get("data", {}).get("text", ""))) if seg["type"] == "text" else 1


def split_segments(segments: list[dict], limit: int) -> list[list[dict]]:
    """按文本长度切分消息段，保证单条消息不超过 limit 个字符."""
    if limit <= 0:
        return [segments]
    total = sum(_seg_len(s) for s in segments)
    if total <= limit:
        return [segments]
    batches: list[list[dict]] = []
    current: list[dict] = []
    used = 0
    for seg in segments:
        if seg["type"] != "text":
            current.append(seg)
            used += 1
            continue
        for chunk in _split_text(str(seg["data"].get("text", "")), max(1, limit - used)):
            current.append({"type": "text", "data": {"text": chunk}})
            used += len(chunk)
            if used >= limit:
                batches.append(current)
                current, used = [], 0
    if current:
        batches.append(current)
    return batches or [segments]


def _split_text(text: str, limit: int) -> list[str]:
    if limit <= 0 or len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        index = -1
        for sep in ("\n", "。", "！", "？", ". ", "! ", "? ", "；", "; ", " "):
            pos = window.rfind(sep)
            if pos > index:
                index = pos + len(sep)
        if index <= 0:
            index = limit
        chunks.append(rest[:index])
        rest = rest[index:]
    if rest:
        chunks.append(rest)
    return chunks


def segments_to_text(segments: list[dict]) -> str:
    """官方 QQ 机器人通道只吃文本，这里把消息段压成纯文本.

    图片/视频等无法直接发送（官方需先上传拿 file_info），用文字占位提示。
    """
    parts: list[str] = []
    for seg in segments:
        seg_type = seg.get("type")
        data = seg.get("data") or {}
        if seg_type == "text":
            parts.append(str(data.get("text", "")))
        elif seg_type == "at":
            qq = str(data.get("qq", ""))
            parts.append("全体成员" if qq == "all" else f"@{qq}")
        elif seg_type == "image":
            parts.append(f"[图片] {data.get('file', data.get('url', ''))}".rstrip())
        elif seg_type == "record":
            parts.append("[语音]")
        elif seg_type == "video":
            parts.append("[视频]")
        elif seg_type == "face":
            parts.append("[表情]")
        else:
            parts.append(f"[{seg_type}]")
    return "".join(parts).strip()


def to_plain_text(segments: list[dict]) -> str:
    """把消息段压成纯文本，仅用于日志/失败记录."""
    return segments_to_text(segments)


def build_segments(
    message: Any,
    *,
    prefix: str = "",
    title: str = "",
    convert_markdown: bool = True,
    wrap_code_block: bool = True,
) -> list[dict]:
    """构造最终发送的消息段：可用 prefix 固定头部、title 作为标题行.

    头部（prefix/title）当作纯文本处理并转义，不参与 markdown 转换，
    避免其中的 [] 被 markdown 链接语法误伤。
    """
    segments = normalize_message(message, convert_markdown=convert_markdown)
    head = prefix
    if title:
        if not head:
            head = f"【{title}】"
        else:
            head = head if head.endswith((" ", "\n")) else head + " "
            head = f"{head}【{title}】"
    if head and not head.endswith(("\n", " ")):
        head += "\n"
    head_segments = text_to_segments(head) if head else []

    # 纯文本时包一层代码块，QQ 里等宽显示、CQ 码也不会被解析
    if (
        wrap_code_block
        and isinstance(message, str)
        and len(segments) == 1
        and segments[0]["type"] == "text"
        and segments[0]["data"]["text"]
    ):
        body = segments[0]["data"]["text"]
        segments = text_to_segments(f"```\n{body}\n```")
    return head_segments + segments
