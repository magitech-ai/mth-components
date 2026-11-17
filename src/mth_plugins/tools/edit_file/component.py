"""Anima component entry point for edit_file tool."""

from __future__ import annotations

from typing import Any

from anima import Context
from magitech_harness.tools.definition import define_tool

from mth_plugins.tools.edit_file.edit_file import handle_edit_file

EDIT_SECTION_TEXT = (
    "Use the edit_file tool to replace a unique target string in a file. The "
    "target must occur exactly once; use write_file to replace an entire file."
)


def apply(ctx: Context, config: dict[str, Any] | None = None, **kwargs: Any) -> None:
    """Mounts edit_file tool onto ctx.tools.

    Registers ``tool:edit_file`` system prompt guidance (reference
    fs/tool-fs edit tool).
    """
    edit_tool = define_tool(
        name="edit_file",
        description="Replace target string with replacement string in a file.",
        parameters={
            "path": {
                "type": "string",
                "description": "Target file path",
                "required": True,
            },
            "target": {
                "type": "string",
                "description": "Exact text to replace",
                "required": True,
            },
            "replacement": {
                "type": "string",
                "description": "Replacement text",
                "required": True,
            },
        },
        execute=handle_edit_file,
    )
    ctx.tools.register(edit_tool)

    # Register system prompt guidance for the edit tool (reference fs/tool-fs).
    system_prompt = ctx.get("systemPrompt", None, strict=False)
    if system_prompt is not None:
        system_prompt.section(
            {
                "name": "tool:edit_file",
                "order": system_prompt.get_section_order("TOOL_EDIT"),
                "text": EDIT_SECTION_TEXT,
            }
        )


apply.inject = ["tools"]  # type: ignore[attr-defined]
