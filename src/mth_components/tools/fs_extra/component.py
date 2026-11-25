"""Animas component entry point for extra filesystem tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from animas import Context
from magitech_harness.tools.schema import define_tool

from mth_components.tools.fs_extra.handlers import (
    handle_delete_file,
    handle_list_directory,
)


def apply(ctx: Context, config: dict | None = None, **kwargs: Any) -> None:
    cfg = {**(config or {}), **kwargs}
    configured_root = cfg.get("workspace_root")

    def _root() -> Path | str | None:
        return configured_root or getattr(ctx, "workspace_dir", None)

    async def _delete(args: dict[str, Any]) -> str:
        return handle_delete_file(args, _root())

    async def _list(args: dict[str, Any]) -> str:
        return handle_list_directory(args, _root())

    ctx.tools.register(
        define_tool(
            name="delete_file",
            description="Delete a file. Only files that exist can be deleted.",
            parameters={"path": {"type": "string", "required": True}},
            execute=_delete,
        )
    )
    ctx.tools.register(
        define_tool(
            name="list_directory",
            description="List the entries of a directory.",
            parameters={"path": {"type": "string"}},
            execute=_list,
        )
    )


apply.inject = ["tools"]  # type: ignore[attr-defined]
