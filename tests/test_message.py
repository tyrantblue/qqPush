"""消息构造 / markdown 降级 / CQ 码转义 单元测试."""

from __future__ import annotations

import pytest

from qqpush.message import (
    BadRequest,
    build_segments,
    normalize_message,
    split_segments,
    to_plain_text,
)
from qqpush.util import escape_cq, markdown_to_text, split_text


class TestEscape:
    def test_escape_cq_special_chars(self):
        assert escape_cq("a&b[c]d,e") == "a&amp;b&#91;c&#93;d&#44;e"

    def test_escape_keeps_plain_text(self):
        assert escape_cq("普通文本 123") == "普通文本 123"


class TestMarkdown:
    def test_bold_and_inline_code(self):
        text = markdown_to_text("**粗体** 和 `code`")
        assert text == "粗体 和 code"

    def test_link_and_image(self):
        text = markdown_to_text("[首页](https://a.com) ![图](https://b.com/x.png)")
        assert "首页 (https://a.com)" in text
        assert "[图片:图] https://b.com/x.png" in text

    def test_heading_and_bullet(self):
        text = markdown_to_text("# 标题\n- 一\n- 二")
        assert "【标题】" in text
        assert "· 一" in text

    def test_code_block_unwrapped(self):
        text = markdown_to_text("```python\nprint(1)\n```")
        assert text == "print(1)"

    def test_blank_lines_collapsed(self):
        assert "\n\n\n" not in markdown_to_text("a\n\n\n\nb")


class TestNormalize:
    def test_plain_text_is_escaped(self):
        segments = normalize_message("收到 [CQ:at,qq=1] 消息")
        assert segments[0]["type"] == "text"
        assert "[CQ:at" not in segments[0]["data"]["text"]
        assert "&#91;CQ:at" in segments[0]["data"]["text"]

    def test_markdown_disabled_keeps_raw(self):
        segments = normalize_message("**粗体**", convert_markdown=False)
        assert segments[0]["data"]["text"] == "**粗体**"

    def test_markdown_enabled_downgrades(self):
        segments = normalize_message("**粗体**", convert_markdown=True)
        assert segments[0]["data"]["text"] == "粗体"

    def test_segment_array_passthrough(self):
        segments = normalize_message(
            [
                {"type": "text", "data": {"text": "hi"}},
                {"type": "image", "data": {"file": "http://x/1.png"}},
            ]
        )
        assert segments[1] == {"type": "image", "data": {"file": "http://x/1.png"}}

    def test_unknown_segment_rejected(self):
        with pytest.raises(BadRequest):
            normalize_message([{"type": "shell", "data": {"cmd": "rm -rf /"}}])

    def test_empty_rejected(self):
        with pytest.raises(BadRequest):
            normalize_message("   ")
        with pytest.raises(BadRequest):
            normalize_message([])

    def test_non_string_rejected(self):
        with pytest.raises(BadRequest):
            normalize_message(123)


class TestBuildSegments:
    def test_prefix_and_title(self):
        segments = build_segments("正文", prefix="[监控] ", title="告警")
        text = to_plain_text(segments)
        # 头部按纯文本转义，避免 [ ] 被当作 CQ 码/链接语法
        assert text.startswith("&#91;监控&#93; 【告警】")
        assert "正文" in text

    def test_title_without_prefix(self):
        segments = build_segments("正文", title="告警", wrap_code_block=False)
        assert to_plain_text(segments) == "【告警】\n正文"
        assert [s["type"] for s in segments] == ["text", "text"]

    def test_code_block_wrapping(self):
        segments = build_segments("abc", wrap_code_block=True)
        assert to_plain_text(segments) == "```\nabc\n```"

    def test_code_block_not_applied_to_segments(self):
        segments = build_segments([{"type": "image", "data": {"file": "x"}}], wrap_code_block=True)
        assert segments == [{"type": "image", "data": {"file": "x"}}]


class TestSplit:
    def test_split_segments_respects_limit(self):
        segments = [{"type": "text", "data": {"text": "a" * 10}}]
        batches = split_segments(segments, 4)
        assert len(batches) == 3
        assert all(sum(len(s["data"]["text"]) for s in b) <= 4 for b in batches)
        assert "".join(s["data"]["text"] for b in batches for s in b) == "a" * 10

    def test_split_segments_keeps_short_message(self):
        segments = [{"type": "text", "data": {"text": "hi"}}]
        assert split_segments(segments, 100) == [segments]

    def test_split_prefers_newline(self):
        batches = split_segments([{"type": "text", "data": {"text": "12345\n67890"}}], 7)
        assert batches[0][0]["data"]["text"] == "12345\n"

    def test_split_text_helper(self):
        chunks = split_text("hello world foo", 8)
        assert all(len(c) <= 8 for c in chunks)
        assert "".join(chunks).replace(" ", "") == "helloworldfoo"
