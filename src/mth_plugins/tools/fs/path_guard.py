"""Workspace-rooted path confinement for filesystem tools.

Collapses ``..`` and resolves symlinks before checking containment, matching
the reference's realpath-based workspace boundary. An escape raises
:class:`PathEscapeError` with a clear message; callers surface it as an error.
"""

from __future__ import annotations

from pathlib import Path


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


__all__ = ["PathEscapeError", "confined_path"]
