"""Anima component entry point for the fs-search tools (glob + grep)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from anima import Context
from magitech_harness.tools.schema import define_tool

from mth_plugins.tools.fs_search.glob import DEFAULT_MAX_RESULTS, handle_glob
from mth_plugins.tools.fs_search.grep import (
    DEFAULT_MAX_LINE_BYTES,
    DEFAULT_MAX_MATCHES,
    handle_grep,
)

GLOB_SECTION_TEXT = (
    "Use the glob tool — not shell find — to discover files by path pattern. "
    "A pattern with no slash matches the basename at any depth; include a "
    "separator to anchor the depth."
)
GREP_SECTION_TEXT = (
    "Use the grep tool — not shell grep or rg — to search file contents. "
    "Use read_file on a matched file when you need surrounding context."
)


def apply(ctx: Context, config: dict[str, Any] | None = None, **kwargs: Any) -> None:
    """Mount glob and grep onto ctx.tools (Tier 1 fs-search parity)."""
    cfg = {**(config or {}), **kwargs}
    max_results = int(cfg.get("globMaxResults", DEFAULT_MAX_RESULTS))
    max_matches = int(cfg.get("grepMaxMatches", DEFAULT_MAX_MATCHES))
    max_line_bytes = int(cfg.get("grepMaxLineBytes", DEFAULT_MAX_LINE_BYTES))
    configured_root = cfg.get("workspace_root")

    def _root() -> Path | str | None:
        return configured_root or getattr(ctx, "workspace_dir", None)

    async def _glob(args: dict[str, Any]) -> str:
        return handle_glob(args, workspace_root=_root(), max_results=max_results)

    async def _grep(args: dict[str, Any]) -> str:
        return handle_grep(
            args,
            workspace_root=_root(),
            max_matches=max_matches,
            max_line_bytes=max_line_bytes,
        )

    ctx.tools.register(
        define_tool(
            name="glob",
            description=(
                "Find files, not directories, whose paths match a glob pattern, "
                f"including hidden files. Returns up to {max_results} paths in "
                "modification-time order."
            ),
            parameters={
                "pattern": {
                    "type": "string",
                    "description": (
                        "Glob pattern to match file paths (e.g. '**/*.py'). A pattern "
                        "with no slash matches the basename at any depth."
                    ),
                    "required": True,
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Directory to search in. Defaults to the session workspace; "
                        "a relative path resolves against it."
                    ),
                },
            },
            execute=_glob,
        )
    )
    ctx.tools.register(
        define_tool(
            name="grep",
            description=(
                "Search file contents with a regular expression. Returns matching "
                "lines with line numbers, grouped by file, up to "
                f"{max_matches} matches."
            ),
            parameters={
                "pattern": {
                    "type": "string",
                    "description": "Regular expression to search for.",
                    "required": True,
                },
                "path": {
                    "type": "string",
                    "description": (
                        "File or directory to search. Defaults to the "
                        "session workspace."
                    ),
                },
                "include": {
                    "type": "string",
                    "description": (
                        "One glob filter for which files to search (e.g. '*.py')."
                    ),
                },
            },
            execute=_grep,
        )
    )

    system_prompt = ctx.get("systemPrompt", None, strict=False)
    if system_prompt is not None:  # pragma: no branch - tools inject systemPrompt
        system_prompt.section(
            {
                "name": "tool:glob",
                "order": system_prompt.get_section_order("TOOL_GLOB"),
                "text": GLOB_SECTION_TEXT,
            }
        )
        system_prompt.section(
            {
                "name": "tool:grep",
                "order": system_prompt.get_section_order("TOOL_GREP"),
                "text": GREP_SECTION_TEXT,
            }
        )


apply.inject = ["tools"]  # type: ignore[attr-defined]
