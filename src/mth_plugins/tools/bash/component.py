"""Anima component entry point for bash tool."""

from __future__ import annotations

from typing import Any

from anima import Context
from magitech_harness.tools.schema import define_tool

from mth_plugins.tools.bash.bash import handle_bash

BASH_SECTION_TEXT = (
    "Use the bash tool to execute shell commands. Commands run in a "
    "subprocess with a configurable timeout; large output is spilled to disk. "
    "Prefer read_file/write_file/edit_file for file access over shell commands."
)


def apply(ctx: Context, **kwargs: Any) -> None:
    """Mounts bash tool onto ctx.tools.

    Registers ``tool:bash`` system prompt guidance.
    """

    async def _execute_bash(args: dict[str, Any]) -> str:
        return await handle_bash(args, ctx=ctx)

    bash_tool = define_tool(
        name="bash",
        description="Execute a bash command in a subprocess.",
        parameters={
            "command": {
                "type": "string",
                "description": "Shell command to execute",
                "required": True,
            },
            "timeout_ms": {
                "type": "integer",
                "description": "Timeout in milliseconds (default 30000)",
            },
            "cwd": {
                "type": "string",
                "description": "Optional working directory",
            },
        },
        execute=_execute_bash,
    )
    ctx.tools.register(bash_tool)

    # Register system prompt guidance for the bash tool.
    system_prompt = ctx.get("systemPrompt", None, strict=False)
    if system_prompt is not None:
        system_prompt.section(
            {
                "name": "tool:bash",
                "order": system_prompt.get_section_order("TOOL_BASH"),
                "text": BASH_SECTION_TEXT,
            }
        )


apply.inject = ["tools"]  # type: ignore[attr-defined]
