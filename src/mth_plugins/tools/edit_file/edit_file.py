"""Filesystem tool handler for editing files."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


async def handle_edit_file(args: dict[str, Any]) -> str:
    """Replaces target string with replacement string in the specified file.

    Matches the reference fs/tool-fs edit tool: a blank path, empty target,
    or target equal to replacement (no-op) are all rejected; the target must
    match exactly once.
    """
    path_str = args.get("path")
    if not path_str or not path_str.strip():
        return "Error: 'path' argument is required."
    target = args.get("target")
    if target is None or target == "":
        return "Error: 'target' argument is required."
    replacement = args.get("replacement", "")

    if target == replacement:
        return f"Error: target and replacement must differ in {path_str}"

    file_path = Path(path_str)
    if not file_path.exists():
        return f"Error: File not found: {path_str}"

    def _perform_edit() -> str:
        content = file_path.read_text(encoding="utf-8")
        count = content.count(target)
        if count == 0:
            return f"Error: Target string not found in {path_str}"
        if count > 1:
            return (
                f"Error: Target string matched {count} times in {path_str}. "
                "Must match uniquely (exactly 1 occurrence)."
            )

        new_content = content.replace(target, replacement, 1)
        file_path.write_text(new_content, encoding="utf-8")
        return f"Successfully updated {path_str}"

    return await asyncio.to_thread(_perform_edit)
