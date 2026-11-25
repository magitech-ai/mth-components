"""Animas component entry point for CodeAct tool execution."""

from __future__ import annotations

from typing import Any

from animas import Context
from magitech_harness.tools.schema import define_tool

from mth_components.tools.codeact.codeact import CodeActExecutor


def apply(ctx: Context, **kwargs: Any) -> CodeActExecutor:
    """Mounts the CodeAct Python execution tool onto the Animas context."""
    executor = CodeActExecutor(ctx)

    async def _handle_execute_python(args: dict[str, Any]) -> str:
        code = str(args.get("code", ""))
        if not code.strip():
            return "Error: 'code' argument is required."
        return await executor.execute(code)

    tool = define_tool(
        name="execute_python",
        description=(
            "Execute Python code in a safe CodeAct environment with persistent "
            "session locals, tool invocations, and final_answer(value)."
        ),
        parameters={
            "code": {
                "type": "string",
                "description": "Python source code to execute.",
                "required": True,
            }
        },
        execute=_handle_execute_python,
    )

    tools_svc = getattr(ctx, "tools", None)
    if tools_svc is not None and hasattr(tools_svc, "register"):
        tools_svc.register(tool)

    # Dispose the runner subprocess when the component's fiber unloads.
    ctx.fiber.effect(executor.dispose, label="codeact.executor.dispose")

    return executor


apply.inject = ["tools", "sandbox"]  # type: ignore[attr-defined]
