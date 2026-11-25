"""Unit tests for repeat-tool-reminder guardrail."""

from __future__ import annotations

import pytest
from animas import Context
from magitech_harness.tools.service import ToolService

from mth_components.guards.repeat_tool_reminder.component import apply
from mth_components.guards.repeat_tool_reminder.guard import (
    RepeatToolReminder,
    canonical_arguments,
)


def test_canonical_arguments_key_order_independence() -> None:
    """Canonical arguments ignore dict key order."""
    a = canonical_arguments({"b": 2, "a": 1})
    b = canonical_arguments({"a": 1, "b": 2})
    assert a == b


def test_canonical_arguments_nested() -> None:
    """Nested dicts are deep-key-sorted."""
    a = canonical_arguments({"z": {"y": 1, "x": 2}})
    b = canonical_arguments({"z": {"x": 2, "y": 1}})
    assert a == b


def test_default_thresholds() -> None:
    """Default thresholds are [3, 5, 8]."""
    reminder = RepeatToolReminder()
    assert reminder._thresholds == [3, 5, 8]


def test_custom_thresholds() -> None:
    """Custom thresholds are accepted and sorted."""
    reminder = RepeatToolReminder(thresholds=[5, 2, 3])
    assert reminder._thresholds == [2, 3, 5]


def test_empty_thresholds_rejected() -> None:
    """Empty thresholds list raises ValueError."""
    with pytest.raises(ValueError, match="must not be empty"):
        RepeatToolReminder(thresholds=[])


def test_threshold_below_two_rejected() -> None:
    """Threshold below 2 raises ValueError."""
    with pytest.raises(ValueError, match="must be >= 2"):
        RepeatToolReminder(thresholds=[1])


def test_duplicate_thresholds_rejected() -> None:
    """Duplicate thresholds raise ValueError."""
    with pytest.raises(ValueError, match="must be unique"):
        RepeatToolReminder(thresholds=[3, 3])


def test_record_first_call_returns_none() -> None:
    """First call for a signature returns None (no reminder yet)."""
    reminder = RepeatToolReminder(thresholds=[3])
    result = reminder.record("agent-1", "bash", {"cmd": "ls"})
    assert result is None


def test_record_threshold_reached() -> None:
    """Third consecutive call returns count 3."""
    reminder = RepeatToolReminder(thresholds=[3])
    reminder.record("agent-1", "bash", {"cmd": "ls"})
    reminder.record("agent-1", "bash", {"cmd": "ls"})
    result = reminder.record("agent-1", "bash", {"cmd": "ls"})
    assert result == 3


def test_record_different_args_independent_chains() -> None:
    """Each (tool, args) signature has its own independent count."""
    reminder = RepeatToolReminder(thresholds=[2])
    reminder.record("agent-1", "bash", {"cmd": "ls"})
    # Different args -> separate signature, starts at 1
    result = reminder.record("agent-1", "bash", {"cmd": "pwd"})
    assert result is None
    # Same ls args again -> count is 2 (independent from pwd)
    result = reminder.record("agent-1", "bash", {"cmd": "ls"})
    assert result == 2


def test_record_different_agent_independent() -> None:
    """Repeat chains are independent per agent."""
    reminder = RepeatToolReminder(thresholds=[2])
    reminder.record("agent-1", "bash", {"cmd": "ls"})
    # Agent-2's first call is independent
    result = reminder.record("agent-2", "bash", {"cmd": "ls"})
    assert result is None


def test_include_filter() -> None:
    """Only included tools are tracked."""
    reminder = RepeatToolReminder(thresholds=[2], include=["read_file"])
    # Not in include list -> not tracked
    result = reminder.record("agent-1", "bash", {"cmd": "ls"})
    assert result is None
    # In include list -> tracked
    reminder.record("agent-1", "read_file", {"path": "/tmp/x"})
    result = reminder.record("agent-1", "read_file", {"path": "/tmp/x"})
    assert result == 2


def test_exclude_filter() -> None:
    """Excluded tools are never tracked."""
    reminder = RepeatToolReminder(thresholds=[2], exclude=["todo_write"])
    # Excluded -> not tracked even on repeat
    result = reminder.record("agent-1", "todo_write", {"text": "x"})
    assert result is None
    result = reminder.record("agent-1", "todo_write", {"text": "x"})
    assert result is None


def test_reset_agent() -> None:
    """reset_agent clears the chain for one agent."""
    reminder = RepeatToolReminder(thresholds=[2])
    reminder.record("agent-1", "bash", {"cmd": "ls"})
    assert reminder.record("agent-1", "bash", {"cmd": "ls"}) == 2
    reminder.reset_agent("agent-1")
    # After reset, count starts fresh
    result = reminder.record("agent-1", "bash", {"cmd": "ls"})
    assert result is None


def test_reminder_text_first_threshold() -> None:
    """First threshold produces the generic nudge message."""
    reminder = RepeatToolReminder(thresholds=[3])
    text = reminder.reminder_text(3, "bash", {"cmd": "ls"})
    assert "repeating the exact same tool call" in text
    assert "analyze the previous result" in text


def test_reminder_text_later_threshold() -> None:
    """Later thresholds produce the detailed message with arguments."""
    reminder = RepeatToolReminder(thresholds=[3, 5, 8])
    text = reminder.reminder_text(5, "bash", {"cmd": "ls -la"})
    assert "Repeated tool call detected" in text
    assert "- tool: bash" in text
    assert "- consecutive_calls: 5" in text
    assert "- arguments:" in text


def test_reminder_text_argument_preview_truncation() -> None:
    """Long arguments are truncated at arguments_preview_chars."""
    reminder = RepeatToolReminder(thresholds=[3, 5], arguments_preview_chars=10)
    long_args = {"cmd": "x" * 100}
    text = reminder.reminder_text(5, "bash", long_args)
    assert "… (+" in text and "more chars)" in text
    # The preview should be shorter than the full canonical JSON
    full = canonical_arguments(long_args)
    assert len(text.split("- arguments: ")[1].split("\n")[0]) < len(full)


def test_chains_copy() -> None:
    """chains() returns a shallow copy, not the internal dict."""
    reminder = RepeatToolReminder(thresholds=[3])
    reminder.record("agent-1", "bash", {"cmd": "ls"})
    chains = reminder.chains()
    assert "agent-1" in chains
    # Mutating the copy doesn't affect the original
    chains["agent-1"].clear()
    assert reminder.chains()["agent-1"] != {}


def test_component_apply_registers(ctx: Context) -> None:
    """apply() registers hooks on the context."""
    # Just verify apply doesn't raise
    apply(ctx, thresholds=[2])


@pytest.fixture
def ctx() -> Context:
    return Context()


def test_component_apply_defaults(ctx: Context) -> None:
    """apply() with no config uses default thresholds."""
    apply(ctx)
    # Should not raise


def test_record_excluded_tool_neither_increments_nor_resets() -> None:
    """Excluded tool interleaved between repeats doesn't launder the chain."""
    reminder = RepeatToolReminder(thresholds=[2], exclude=["todo_write"])
    # bash tracked
    reminder.record("agent-1", "bash", {"cmd": "ls"})
    # todo_write excluded - neither increments nor resets
    reminder.record("agent-1", "todo_write", {"text": "x"})
    # bash still at count 2
    result = reminder.record("agent-1", "bash", {"cmd": "ls"})
    assert result == 2


@pytest.mark.asyncio
async def test_waterfall_post_execute_adds_reminder() -> None:
    """tools/post-execute waterfall injects reminder context on repeat."""
    ctx = Context()
    ctx.use(ToolService)
    await ctx.flush()
    apply(ctx, thresholds=[2])

    exec_view = {
        "name": "bash",
        "callId": "call-1",
        "arguments": {"cmd": "ls"},
        "agent": "agent-1",
    }
    frozen_input = {"isError": False, "content": [], "value": None}

    # First call: no reminder
    result = await ctx.waterfall(
        "tools/post-execute",
        exec_view,
        frozen_input,
        default_handler=lambda *_a: {"kind": "accept"},
    )
    contexts = result.get("additionalContexts") or []
    assert len(contexts) == 0

    # Second call with same args: reminder at threshold 2
    result = await ctx.waterfall(
        "tools/post-execute",
        exec_view,
        frozen_input,
        default_handler=lambda *_a: {"kind": "accept"},
    )
    contexts = result.get("additionalContexts", [])
    assert len(contexts) == 1
    assert contexts[0]["kind"] == "repeat_tool_reminder"
    assert contexts[0]["summary"] == "bash x 2"
    assert "repeating the exact same tool call" in contexts[0]["content"][0]["text"]


@pytest.mark.asyncio
async def test_waterfall_pre_step_clears_chain() -> None:
    """agent/pre-step clears the per-agent repeat chain."""
    ctx = Context()
    ctx.use(ToolService)
    await ctx.flush()
    apply(ctx, thresholds=[2])

    exec_view = {
        "name": "bash",
        "callId": "call-1",
        "arguments": {"cmd": "ls"},
        "agent": "agent-1",
    }
    frozen_input = {"isError": False, "content": [], "value": None}

    # Build up to threshold
    await ctx.waterfall(
        "tools/post-execute",
        exec_view,
        frozen_input,
        default_handler=lambda *_a: {"kind": "accept"},
    )
    result = await ctx.waterfall(
        "tools/post-execute",
        exec_view,
        frozen_input,
        default_handler=lambda *_a: {"kind": "accept"},
    )
    contexts = result.get("additionalContexts", [])
    assert len(contexts) == 1  # threshold reached

    # New user prompt clears the chain
    await ctx.waterfall(
        "agent/pre-step",
        {"agent": "agent-1"},
        default_handler=lambda *_a: None,
    )

    # After reset, first call again produces no reminder
    result = await ctx.waterfall(
        "tools/post-execute",
        exec_view,
        frozen_input,
        default_handler=lambda *_a: {"kind": "accept"},
    )
    contexts = result.get("additionalContexts", [])
    assert len(contexts) == 0


@pytest.mark.asyncio
async def test_waterfall_post_execute_no_agent_passthrough() -> None:
    """exec_view without agent passes through unchanged."""
    ctx = Context()
    ctx.use(ToolService)
    await ctx.flush()
    apply(ctx, thresholds=[2])

    exec_view = {
        "name": "bash",
        "callId": "call-1",
        "arguments": {"cmd": "ls"},
        # no agent
    }
    frozen_input = {"isError": False, "content": [], "value": None}

    result = await ctx.waterfall(
        "tools/post-execute",
        exec_view,
        frozen_input,
        default_handler=lambda *_a: {"kind": "accept"},
    )
    assert (
        result.get("additionalContexts") is None
        or len(result.get("additionalContexts", [])) == 0
    )


@pytest.mark.asyncio
async def test_waterfall_post_execute_no_tool_name_passthrough() -> None:
    """exec_view without tool name passes through unchanged."""
    ctx = Context()
    ctx.use(ToolService)
    await ctx.flush()
    apply(ctx, thresholds=[2])

    exec_view = {
        # no name
        "callId": "call-1",
        "arguments": {"cmd": "ls"},
        "agent": "agent-1",
    }
    frozen_input = {"isError": False, "content": [], "value": None}

    result = await ctx.waterfall(
        "tools/post-execute",
        exec_view,
        frozen_input,
        default_handler=lambda *_a: {"kind": "accept"},
    )
    assert (
        result.get("additionalContexts") is None
        or len(result.get("additionalContexts", [])) == 0
    )


@pytest.mark.asyncio
async def test_waterfall_pre_step_no_agent_passthrough() -> None:
    """agent/pre-step without agent passes through unchanged."""
    ctx = Context()
    ctx.use(ToolService)
    await ctx.flush()
    apply(ctx, thresholds=[2])

    # No agent in step data
    result = await ctx.waterfall(
        "agent/pre-step",
        {},
        default_handler=lambda *_a: None,
    )
    assert result is None
