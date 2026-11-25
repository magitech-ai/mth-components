"""Animas component entry point for read_file tool."""

from __future__ import annotations

from typing import Any

from animas import Context
from magitech_harness.tools.schema import define_tool

from mth_components.tools.read_file.read_file import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    handle_read_file,
)

READ_SECTION_TEXT = (
    "Use the read_file tool — not shell commands like cat, sed, head, or tail — "
    "to inspect text files. Use offset and limit to read a line range or continue "
    "a large file. Results are capped at 2000 lines per call."
)


def apply(ctx: Context, config: dict[str, Any] | None = None, **kwargs: Any) -> None:
    """Mounts read_file tool onto ctx.tools.

    Reads ``default_limit``/``max_limit`` from component config (defaults 2000),
    enforcing a per-call line cap on file reads.
    Also registers ``tool:read`` system prompt guidance.
    """
    cfg = {**(config or {}), **kwargs}
    default_limit = int(cfg.get("default_limit", DEFAULT_LIMIT))
    max_limit = int(cfg.get("max_limit", MAX_LIMIT))

    configured_root = cfg.get("workspace_root")

    async def _execute(args: dict[str, Any]) -> str:
        # Resolve the workspace root at call time so branch-worktree children
        # confine to their own workspace, not a captured parent root.
        root = configured_root or getattr(ctx, "workspace_dir", None)
        return await handle_read_file(
            args,
            default_limit=default_limit,
            max_limit=max_limit,
            workspace_root=root,
        )

    read_tool = define_tool(
        name="read_file",
        description="Read file contents with line offset and limit pagination.",
        parameters={
            "path": {"type": "string", "description": "Path to file", "required": True},
            "offset": {"type": "integer", "description": "1-indexed starting line"},
            "limit": {
                "type": "integer",
                "description": (
                    f"Maximum lines to read (default {default_limit}, max {max_limit})"
                ),
            },
        },
        execute=_execute,
    )
    ctx.tools.register(read_tool)

    # Register system prompt guidance for the read tool (reference fs/tool-fs).
    system_prompt = ctx.get("systemPrompt", None, strict=False)
    if system_prompt is not None:
        system_prompt.section(
            {
                "name": "tool:read_file",
                "order": system_prompt.get_section_order("TOOL_READ"),
                "text": READ_SECTION_TEXT,
            }
        )


apply.inject = ["tools"]  # type: ignore[attr-defined]
