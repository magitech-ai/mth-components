"""Workspace-rooted path confinement for marketplace filesystem extras.

A private copy: this repository may only import *released* harness surface, and
this helper is used by `fs_extra` alone. The harness owns the copy its base
tools use (`magitech_harness.tools.path_guard`).
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["PathEscapeError", "confined_path"]


class PathEscapeError(ValueError):
    """Raised when a requested path resolves outside the workspace root."""


def confined_path(root: Path | str, raw: str) -> Path:
    """Resolve *raw* under *root* and reject any path that escapes it."""
    base = Path(root).resolve(strict=True)
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    resolved = candidate.resolve(strict=False)
    if resolved != base and base not in resolved.parents:
        raise PathEscapeError(f"path escapes workspace: {raw}")
    return resolved
