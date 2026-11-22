"""Unit tests for CodeAct component registration."""

import pytest
from anima import Context
from magitech_harness.tools.service import ToolCallData, ToolResultData, ToolService

from mth_plugins.tools.codeact.component import apply


@pytest.mark.asyncio
async def test_component_apply_and_execute() -> None:
    ctx = Context()
    tools = ToolService(ctx)

    executor = apply(ctx)
    assert executor is not None

    tool = tools.get("execute_python")
    assert tool is not None
    assert "code" in tool["parameters"]["properties"]

    call = ToolCallData(
        call_id="c_python",
        name="execute_python",
        arguments={"code": "res = 10 * 10\nres"},
    )
    result = await tools.execute(call)
    assert isinstance(result, ToolResultData)
    assert result.is_error is False
    assert result.content == "100"


@pytest.mark.asyncio
async def test_component_rejects_blank_code() -> None:
    """Verifies whitespace-only code is rejected without spawning a subprocess."""
    ctx = Context()
    tools = ToolService(ctx)
    apply(ctx)
    call = ToolCallData(
        call_id="c_blank",
        name="execute_python",
        arguments={"code": "   \n  \t  "},
    )
    result = await tools.execute(call)
    assert result.is_error is False
    assert "required" in result.content


def test_component_apply_without_tool_service() -> None:
    ctx = Context()
    executor = apply(ctx)
    assert executor is not None
