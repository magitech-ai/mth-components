"""Anima component entry point for developer workflow tools."""

from __future__ import annotations

from typing import Any

from anima import Context
from magitech_harness.tools.schema import define_tool

from mth_plugins.tools.dev_tools.handlers import handle_add_repo, handle_reload_module


def apply(ctx: Context, config: dict | None = None, **kwargs: Any) -> None:
    cfg = {**(config or {}), **kwargs}
    home_dir = cfg.get("home_dir")

    async def _reload(args: dict[str, Any], run_ctx: Any) -> str:
        return handle_reload_module(args, run_ctx)

    async def _add(args: dict[str, Any]) -> str:
        return handle_add_repo(args, home_dir)

    ctx.tools.register(
        define_tool(
            name="reload_module",
            description=(
                "Hot-reload a Python module from disk after reading it this turn."
            ),
            parameters={
                "module_name": {"type": "string", "required": True},
                "file_path": {"type": "string", "required": True},
            },
            execute=_reload,
        )
    )
    ctx.tools.register(
        define_tool(
            name="add_repo",
            description=(
                "Resolve a repo name, clone URL or directory from the home map."
            ),
            parameters={
                "ref": {"type": "string", "required": True},
                "confirm": {"type": "boolean"},
            },
            execute=_add,
        )
    )


apply.inject = ["tools"]  # type: ignore[attr-defined]
