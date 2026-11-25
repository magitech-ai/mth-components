"""Unit tests for timeout-policy guardrail."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from animas import Context
from magitech_harness.agent.cancellation import AbortSignal
from magitech_harness.tools.service import ToolService

from mth_components.guards.timeout_policy.component import apply
from mth_components.guards.timeout_policy.policy import (
    TOOL_TIMEOUT,
    TimeoutPolicy,
)


def test_tool_timeout_constant() -> None:
    """TOOL_TIMEOUT constant is the expected code."""
    assert TOOL_TIMEOUT == "TOOL_TIMEOUT"


def _make_ctx() -> Context:
    ctx = Context()
    ToolService(ctx)
    return ctx


def test_timeout_ms_none_when_tool_not_found() -> None:
    """_timeout_ms returns None when tool is not registered."""
    ctx = _make_ctx()
    tools = ctx.get("tools")
    tools.get = MagicMock(return_value=None)  # type: ignore[method-assign]
    policy = TimeoutPolicy(ctx)
    assert policy._timeout_ms("unknown_tool", None) is None


def test_timeout_ms_none_when_no_timeout_ms() -> None:
    """_timeout_ms returns None when tool has no timeoutMs."""
    ctx = _make_ctx()
    tools = ctx.get("tools")
    tools.get = MagicMock(return_value={"name": "fast_tool"})  # type: ignore[method-assign]
    policy = TimeoutPolicy(ctx)
    assert policy._timeout_ms("fast_tool", None) is None


def test_timeout_ms_from_tool_definition() -> None:
    """_timeout_ms reads timeoutMs from the tool definition."""
    ctx = _make_ctx()
    tools = ctx.get("tools")
    tools.get = MagicMock(return_value={"name": "slow_tool", "timeoutMs": 5000})  # type: ignore[method-assign]
    policy = TimeoutPolicy(ctx)
    assert policy._timeout_ms("slow_tool", None) == 5000.0


@pytest.mark.asyncio
async def test_on_execute_no_timeout_when_fast() -> None:
    """Tool finishing before deadline returns unchanged result."""
    ctx = _make_ctx()
    tools = ctx.get("tools")
    tools.get = MagicMock(return_value={"name": "fast_tool", "timeoutMs": 5000})  # type: ignore[method-assign]
    policy = TimeoutPolicy(ctx)

    exec_view = {
        "name": "fast_tool",
        "arguments": {},
        "agent": None,
        "signal": AbortSignal(),
    }

    async def next_fn(ev: dict[str, Any]) -> dict[str, Any]:
        return {
            "isError": False,
            "value": "ok",
            "content": [{"type": "text", "text": "ok"}],
        }

    result = await policy.on_execute(exec_view, next_fn)
    assert result["isError"] is False
    assert result["value"] == "ok"


@pytest.mark.asyncio
async def test_on_execute_timeout_replaces_result() -> None:
    """Tool exceeding deadline gets structured TOOL_TIMEOUT error."""
    ctx = _make_ctx()
    tools = ctx.get("tools")
    tools.get = MagicMock(return_value={"name": "slow_tool", "timeoutMs": 50})  # type: ignore[method-assign]
    policy = TimeoutPolicy(ctx)

    exec_view = {
        "name": "slow_tool",
        "arguments": {},
        "agent": None,
        "signal": AbortSignal(),
    }

    async def next_fn(ev: dict[str, Any]) -> dict[str, Any]:
        # Simulate the tool taking too long by sleeping beyond the deadline
        await asyncio.sleep(0.2)
        return {
            "isError": False,
            "value": "ok",
            "content": [{"type": "text", "text": "ok"}],
        }

    result = await policy.on_execute(exec_view, next_fn)
    assert result["isError"] is True
    assert result["error"]["code"] == TOOL_TIMEOUT
    assert result["error"]["name"] == "ToolTimeoutError"
    assert "timed out" in result["error"]["reason"]
    assert "timed out" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_on_execute_no_timeout_ms_passthrough() -> None:
    """Tool without timeoutMs is passed through unchanged."""
    ctx = _make_ctx()
    tools = ctx.get("tools")
    tools.get = MagicMock(return_value={"name": "no_limit_tool"})  # type: ignore[method-assign]
    policy = TimeoutPolicy(ctx)

    exec_view = {
        "name": "no_limit_tool",
        "arguments": {},
        "agent": None,
        "signal": AbortSignal(),
    }

    async def next_fn(ev: dict[str, Any]) -> dict[str, Any]:
        return {"isError": False, "value": "ok", "content": []}

    result = await policy.on_execute(exec_view, next_fn)
    assert result["isError"] is False
    assert result["value"] == "ok"


@pytest.mark.asyncio
async def test_waterfall_execute_timeout_replaces_result() -> None:
    """tools/execute waterfall replaces slow result with TOOL_TIMEOUT error."""
    ctx = _make_ctx()
    tools = ctx.get("tools")
    tools.get = MagicMock(return_value={"name": "slow_tool", "timeoutMs": 50})  # type: ignore[method-assign]
    apply(ctx)

    exec_view = {
        "name": "slow_tool",
        "arguments": {},
        "agent": None,
        "signal": AbortSignal(),
    }

    async def default_handler(ev: dict[str, Any]) -> dict[str, Any]:
        # Simulate slow tool by sleeping beyond deadline
        await asyncio.sleep(0.2)
        return {
            "isError": False,
            "value": "ok",
            "content": [{"type": "text", "text": "ok"}],
        }

    result = await ctx.waterfall(
        "tools/execute",
        exec_view,
        default_handler=default_handler,
    )
    assert result["isError"] is True
    assert result["error"]["code"] == TOOL_TIMEOUT
    assert result["error"]["name"] == "ToolTimeoutError"
    assert "timed out" in result["error"]["reason"]
    assert "timed out" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_waterfall_execute_no_timeout_passthrough() -> None:
    """tools/execute waterfall passes through when no timeout configured."""
    ctx = _make_ctx()
    tools = ctx.get("tools")
    tools.get = MagicMock(return_value={"name": "fast_tool"})  # type: ignore[method-assign]
    apply(ctx)

    exec_view = {
        "name": "fast_tool",
        "arguments": {},
        "agent": None,
        "signal": AbortSignal(),
    }

    async def default_handler(ev: dict[str, Any]) -> dict[str, Any]:
        return {
            "isError": False,
            "value": "ok",
            "content": [{"type": "text", "text": "ok"}],
        }

    result = await ctx.waterfall(
        "tools/execute",
        exec_view,
        default_handler=default_handler,
    )
    assert result["isError"] is False
    assert result["value"] == "ok"


def test_component_apply(ctx: Context) -> None:
    """apply() registers the hook without raising."""
    apply(ctx)


@pytest.fixture
def ctx() -> Context:
    return _make_ctx()
