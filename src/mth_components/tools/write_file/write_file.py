"""Filesystem tool handler for writing files."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from mth_components.tools.fs.path_guard import PathEscapeError, confined_path


async def handle_write_file(
    args: dict[str, Any], *, workspace_root: Path | str | None = None
) -> str:
    """Writes content to a file at the given path, creating parent directories.

    Matches the reference fs/tool-fs write tool: a blank (empty or
    whitespace-only) ``file_path`` is rejected; empty ``content`` is
    legitimate (writes an empty file). Paths are confined to the workspace
    root when one is supplied.
    """
    path_str = args.get("path")
    if not path_str or not path_str.strip():
        return "Error: 'path' argument is required."
    content = args.get("content", "")

    if workspace_root is not None:
        try:
            file_path = confined_path(workspace_root, path_str)
        except (PathEscapeError, OSError) as exc:
            return f"Error: {exc}"
    else:
        file_path = Path(path_str)

    def _write() -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")

    await asyncio.to_thread(_write)
    return f"Successfully wrote to {path_str}"
