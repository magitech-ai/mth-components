"""Handlers for the extra filesystem tools (delete_file, list_directory)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from magitech_harness.tools.path_guard import PathEscapeError, confined_path


def _resolve(workspace_root: Path | str | None, path: str) -> str | Path:
    if workspace_root is None:
        return Path(path)
    try:
        return confined_path(workspace_root, path)
    except (PathEscapeError, OSError) as exc:
        return f"Error: {exc}"


def handle_delete_file(args: dict[str, Any], workspace_root: Any = None) -> str:
    path = args.get("path")
    if not path:
        return "Error: 'path' argument is required."
    resolved = _resolve(workspace_root, path)
    if isinstance(resolved, str):
        return resolved
    try:
        resolved.unlink()
    except FileNotFoundError:
        return f"Error: FileNotFoundError: No such file or directory: '{path}'"
    return f"Deleted {path}."


def handle_list_directory(args: dict[str, Any], workspace_root: Any = None) -> str:
    path = args.get("path", ".")
    resolved = _resolve(workspace_root, path)
    if isinstance(resolved, str):
        return resolved
    try:
        entries = sorted(resolved.iterdir())
    except OSError as exc:
        return f"Error: {exc}"
    lines = [f"{e.name}/" if e.is_dir() else e.name for e in entries]
    return "\n".join(lines) if lines else "(empty)"
