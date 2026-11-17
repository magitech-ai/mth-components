"""Unit tests for truncation guardrail and functions in mth-plugins."""

from __future__ import annotations

from typing import Any

import pytest
from anima import Context
from magitech_harness.session.events import ToolCallData
from magitech_harness.system_prompt.service import SystemPromptService
from magitech_harness.tools.definition import define_tool
from magitech_harness.tools.service import ToolService

from mth_plugins.guards.truncation.component import apply
from mth_plugins.guards.truncation.truncation import (
    _tail_suffix,
    format_size,
    truncate_head,
    truncate_tail,
)


def test_format_size() -> None:
    """Verifies byte size formatting across units."""
    assert format_size(500) == "500B"
    assert format_size(2048) == "2.0KB"
    assert format_size(2 * 1024 * 1024) == "2.0MB"


def test_tail_suffix() -> None:
    """Verifies _tail_suffix edge cases."""
    assert _tail_suffix("hello", 100) == "hello"
    assert _tail_suffix("hello", 0) == ""


def test_truncate_head_empty_and_short() -> None:
    """Verifies truncate_head returns untouched result when under limits."""
    empty_res = truncate_head("")
    assert not empty_res.truncated
    assert empty_res.text == ""

    short_text = "line1\nline2\n"
    res = truncate_head(short_text, max_lines=10, max_bytes=100)
    assert not res.truncated
    assert res.text == short_text
    assert res.total_lines == 2


def test_truncate_head_lines_exceeded() -> None:
    """Verifies truncate_head truncates when max_lines is exceeded."""
    text = "line1\nline2\nline3\nline4\n"
    res = truncate_head(text, max_lines=2, max_bytes=1000)
    assert res.truncated
    assert res.strategy == "head"
    assert res.shown_start == 1
    assert res.shown_end == 2
    assert res.text == "line1\nline2"


def test_truncate_head_bytes_exceeded() -> None:
    """Verifies truncate_head truncates when max_bytes is exceeded."""
    text = "line1\nline2\nline3\n"
    res = truncate_head(text, max_lines=10, max_bytes=8)
    assert res.truncated
    assert res.shown_end == 1
    assert res.text == "line1"

    res_zero = truncate_head(text, max_lines=10, max_bytes=2)
    assert res_zero.truncated
    assert res_zero.shown_end is None
    assert res_zero.text == ""


def test_truncate_tail_short_and_exceeded() -> None:
    """Verifies truncate_tail preserves end of content."""
    text = "a\nb\nc\nd\n"
    short_res = truncate_tail(text, max_lines=10, max_bytes=100)
    assert not short_res.truncated
    assert short_res.text == text

    line_res = truncate_tail(text, max_lines=2, max_bytes=100)
    assert line_res.truncated
    assert line_res.strategy == "tail"
    assert line_res.shown_start == 3
    assert line_res.shown_end == 4
    assert line_res.text == "c\nd"

    byte_res = truncate_tail(text, max_lines=10, max_bytes=2)
    assert byte_res.truncated
    assert byte_res.text == "d"

    huge_line = "abcdefghijklmnopqrstuvwxyz"
    tail_res = truncate_tail(huge_line, max_lines=10, max_bytes=5)
    assert tail_res.truncated
    assert tail_res.text == "vwxyz"

    zero_res = truncate_tail(huge_line, max_lines=10, max_bytes=0)
    assert zero_res.truncated
    assert zero_res.text == ""


@pytest.mark.asyncio
async def test_truncation_component_waterfall_integration() -> None:
    """Verifies truncation component attaches to tools/post-execute waterfall."""
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    ctx.use(apply, max_lines=2, max_bytes=50)
    await ctx.flush()

    tool = define_tool(
        name="verbose_tool",
        description="Returns lots of text",
        execute=lambda args: "line1\nline2\nline3\nline4",
    )
    ctx.tools.register(tool)

    call_data = ToolCallData(call_id="call-1", name="verbose_tool", arguments={})
    res = await ctx.tools.execute(call_data)

    assert not res.is_error
    assert "[Output truncated: showing 2 of 4 lines" in res.content
    assert "line1\nline2" in res.content

    short_tool = define_tool(
        name="short_tool",
        description="Returns short text",
        execute=lambda args: "short",
    )
    ctx.tools.register(short_tool)
    call_short = ToolCallData(call_id="call-short", name="short_tool", arguments={})
    res_short = await ctx.tools.execute(call_short)
    assert res_short.content == "short"

    obj_tool = define_tool(
        name="obj_tool",
        description="Returns non-string",
        execute=lambda args: {"count": 42},
    )
    ctx.tools.register(obj_tool)
    call_obj = ToolCallData(call_id="call-2", name="obj_tool", arguments={})
    res_obj = await ctx.tools.execute(call_obj)
    assert res_obj.content == {"count": 42}


@pytest.mark.asyncio
async def test_truncation_new_pipeline_path_truncates_text_blocks() -> None:
    """New pipeline convention: (exec_view, result) -> truncated content."""
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    apply(ctx, max_lines=2, max_bytes=60)
    await ctx.flush()

    exec_view = {"name": "verbose_tool", "callId": "c1"}
    result: dict[str, Any] = {
        "content": [
            {"type": "text", "text": "line1\nline2\nline3\nline4"},
        ],
        "isError": False,
    }

    decision = await ctx.waterfall(
        "tools/post-execute",
        exec_view,
        result,
        default_handler=lambda *_a: {"kind": "accept"},
    )

    assert decision["kind"] == "accept"
    assert len(decision["content"]) == 1
    block = decision["content"][0]
    assert block["type"] == "text"
    assert "line1\nline2" in block["text"]
    assert "[Output truncated" in block["text"]


@pytest.mark.asyncio
async def test_truncation_new_pipeline_path_under_limit_unchanged() -> None:
    """Content within budget passes through unchanged (no-op)."""
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    apply(ctx, max_lines=10, max_bytes=1000)
    await ctx.flush()

    exec_view = {"name": "tool", "callId": "c1"}
    result = {
        "content": [{"type": "text", "text": "short"}],
        "isError": False,
    }

    decision = await ctx.waterfall(
        "tools/post-execute",
        exec_view,
        result,
        default_handler=lambda *_a: {"kind": "accept"},
    )

    # Within budget: content passed through unchanged.
    assert decision["kind"] == "accept"
    assert decision["content"] == result["content"]


@pytest.mark.asyncio
async def test_truncation_new_pipeline_path_non_text_passthrough() -> None:
    """Non-text results (no content list) pass through unchanged."""
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    apply(ctx, max_lines=2, max_bytes=50)
    await ctx.flush()

    exec_view = {"name": "tool", "callId": "c1"}
    result: dict[str, Any] = {"content": "just a string", "isError": False}

    decision = await ctx.waterfall(
        "tools/post-execute",
        exec_view,
        result,
        default_handler=lambda *_a: {"kind": "accept"},
    )

    # Non-text content: passed through unchanged as an accept decision.
    assert decision == {"kind": "accept"}


@pytest.mark.asyncio
async def test_truncation_new_pipeline_path_mixed_blocks() -> None:
    """Non-text blocks are preserved; only text blocks are truncated."""
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    apply(ctx, max_lines=1, max_bytes=40)
    await ctx.flush()

    exec_view = {"name": "tool", "callId": "c1"}
    result: dict[str, Any] = {
        "content": [
            {"type": "text", "text": "alpha\nbeta\ngamma\ndelta"},
            {"type": "image", "attachment": "img-1"},
            {"type": "text", "text": "should be dropped after budget"},
        ],
        "isError": False,
    }

    decision = await ctx.waterfall(
        "tools/post-execute",
        exec_view,
        result,
        default_handler=lambda *_a: {"kind": "accept"},
    )

    assert decision["kind"] == "accept"
    blocks = decision["content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert blocks[0]["text"].startswith("alpha")
    assert "[Output truncated" in blocks[0]["text"]
    # Everything after the truncated block is dropped (legacy-consistent),
    # including the image and the later text block.
    assert not any(b["type"] == "image" for b in blocks)
    assert all(
        b["text"] != "should be dropped after budget"
        for b in blocks
        if b.get("type") == "text"
    )


@pytest.mark.asyncio
async def test_truncation_legacy_path_still_works() -> None:
    """Legacy convention: (output_string, call_data) -> truncated string."""
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    apply(ctx, max_lines=2, max_bytes=60)
    await ctx.flush()

    decision = await ctx.waterfall(
        "tools/post-execute",
        "line1\nline2\nline3\nline4",
        {"call_id": "c1", "name": "tool", "arguments": {}},
        default_handler=lambda res, *a, **k: res,
    )
    assert isinstance(decision, str)
    assert "line1\nline2" in decision
    assert "[Output truncated" in decision


@pytest.mark.asyncio
async def test_truncation_legacy_path_under_limit_passthrough() -> None:
    """Legacy path: output under the budget passes through untruncated."""
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    apply(ctx, max_lines=10, max_bytes=1000)
    await ctx.flush()

    decision = await ctx.waterfall(
        "tools/post-execute",
        "short",
        {"call_id": "c1", "name": "tool", "arguments": {}},
        default_handler=lambda res, *a, **k: res,
    )
    assert decision == "short"


@pytest.mark.asyncio
async def test_truncation_legacy_path_non_string_result_passthrough() -> None:
    """Legacy path: non-string downstream result passes through unchanged."""
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    apply(ctx, max_lines=2, max_bytes=60)
    await ctx.flush()

    decision = await ctx.waterfall(
        "tools/post-execute",
        "line1\nline2",
        {"call_id": "c1", "name": "tool", "arguments": {}},
        default_handler=lambda *a, **k: {"kind": "accept"},
    )
    assert decision == {"kind": "accept"}


@pytest.mark.asyncio
async def test_truncation_new_pipeline_path_non_text_block_appended() -> None:
    """A non-text block before any truncation is appended unchanged (else branch)."""
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    apply(ctx, max_lines=10, max_bytes=1000)
    await ctx.flush()

    exec_view = {"name": "tool", "callId": "c1"}
    result: dict[str, Any] = {
        "content": [
            {"type": "image", "attachment": "img-1"},
            {"type": "text", "text": "short"},
        ],
        "isError": False,
    }
    original_image = result["content"][0]

    decision = await ctx.waterfall(
        "tools/post-execute",
        exec_view,
        result,
        default_handler=lambda *_a: {"kind": "accept"},
    )

    assert decision["kind"] == "accept"
    assert decision["content"][0] is original_image
    assert decision["content"][1]["type"] == "text"
    assert decision["content"][1]["text"] == "short"


@pytest.mark.asyncio
async def test_truncation_new_pipeline_path_idempotent() -> None:
    """Re-truncating already-truncated content does not double-add notices."""
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    apply(ctx, max_lines=1, max_bytes=40)
    await ctx.flush()

    exec_view = {"name": "tool", "callId": "c1"}
    already_truncated = (
        "alpha\n\n[Output truncated: showing 1 of 4 lines (40B limit reached).]"
    )
    already = {
        "content": [
            {
                "type": "text",
                "text": already_truncated,
            }
        ],
        "isError": False,
    }

    first = await ctx.waterfall(
        "tools/post-execute",
        exec_view,
        already,
        default_handler=lambda *_a: {"kind": "accept"},
    )
    second = await ctx.waterfall(
        "tools/post-execute",
        exec_view,
        first,
        default_handler=lambda *_a: {"kind": "accept"},
    )
    assert first["kind"] == "accept"
    assert second["kind"] == "accept"
    # Idempotent: second pass produces no additional truncation notice.
    assert second["content"][0]["text"] == first["content"][0]["text"]
    assert second["content"][0]["text"].count("[Output truncated") == 1
