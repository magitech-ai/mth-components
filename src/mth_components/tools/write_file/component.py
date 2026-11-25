"""Animas component entry point for write_file tool."""

from __future__ import annotations

from typing import Any

from animas import Context
from magitech_harness.tools.schema import define_tool

from mth_components.tools.write_file.write_file import handle_write_file

WRITE_SECTION_TEXT = (
    "Use the write_file tool to create or overwrite a text file. Parent "
    "directories are created automatically. Empty content is legitimate and "
    "writes an empty file; the path must not be blank."
)


def apply(ctx: Context, config: dict[str, Any] | None = None, **kwargs: Any) -> None:
    """Mounts write_file tool onto ctx.tools.

    Registers ``tool:write_file`` system prompt guidance (reference
    fs/tool-fs write tool).
    """
    configured_root = (config or {}).get("workspace_root", kwargs.get("workspace_root"))

    async def _execute(args: dict[str, Any]) -> str:
        root = configured_root or getattr(ctx, "workspace_dir", None)
        return await handle_write_file(args, workspace_root=root)

    write_tool = define_tool(
        name="write_file",
        description="Write text content to a file, creating parent directories.",
        parameters={
            "path": {
                "type": "string",
                "description": "Target file path",
                "required": True,
            },
            "content": {
                "type": "string",
                "description": "File content",
                "required": True,
            },
        },
        execute=_execute,
    )
    ctx.tools.register(write_tool)

    # Register system prompt guidance for the write tool (reference fs/tool-fs).
    system_prompt = ctx.get("systemPrompt", None, strict=False)
    if system_prompt is not None:
        system_prompt.section(
            {
                "name": "tool:write_file",
                "order": system_prompt.get_section_order("TOOL_WRITE"),
                "text": WRITE_SECTION_TEXT,
            }
        )


apply.inject = ["tools"]  # type: ignore[attr-defined]
