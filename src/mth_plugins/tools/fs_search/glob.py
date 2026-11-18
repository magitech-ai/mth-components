"""Pure-Python ``glob`` tool: find files whose paths match a glob pattern.

Python adaptation: the reference drives ripgrep via subprocess; this port uses
``pathlib`` so the wheel stays self-contained and cross-platform. Behavior
(path-pattern discovery, hidden/ignored files included, modification-time
order, cap with full-result spill note) matches the reference's observable
contract. Paths are confined to the workspace root.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mth_plugins.tools.fs.path_guard import PathEscapeError, confined_path

DEFAULT_MAX_RESULTS = 100
_VCS_DIRS = {".git", ".svn", ".hg", ".bzr", ".jj", ".sl"}


def _resolve_root(raw: str | None, workspace_root: Path | str | None) -> Path:
    base = (
        Path(workspace_root).resolve(strict=True)
        if workspace_root
        else Path.cwd().resolve()
    )
    if raw is None or not raw.strip():
        return base
    try:
        return confined_path(base, raw)
    except (PathEscapeError, OSError):  # pragma: no cover - defensive path escape
        return base


def handle_glob(
    args: dict[str, Any],
    *,
    workspace_root: Path | str | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> str:
    """Find files matching a glob pattern under the workspace root."""
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return "Error: 'pattern' argument is required and must be a non-empty string."
    root = _resolve_root(args.get("path"), workspace_root)

    base = root if root.is_dir() else root.parent
    matched: list[Path] = []
    try:
        candidates = base.glob(pattern) if "/" in pattern else base.rglob(pattern)
        for candidate in candidates:
            try:
                if not candidate.is_file():
                    continue
                if any(part in _VCS_DIRS for part in candidate.parts):
                    continue
                resolved = candidate.resolve(strict=False)
                confined_path(workspace_root or base, str(candidate))
            except (PathEscapeError, OSError):  # pragma: no cover - defensive fs error
                continue
            matched.append(resolved)
    except (ValueError, OSError) as exc:  # pragma: no cover - rare pathlib error
        return f"Error: invalid glob pattern: {exc}"

    matched.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    rel = [_relative(root, p).as_posix() for p in matched]
    if not rel:
        return "No files found"
    if len(rel) <= max_results:
        return "\n".join(rel)
    kept = rel[:max_results]
    return (
        "\n".join(kept)
        + f"\n(Showing {len(kept)} of {len(rel)} paths. Full sorted result "
        "was limited by maxResults.)"
    )


def _relative(root: Path, target: Path) -> Path:
    try:
        return target.relative_to(root)
    except ValueError:
        return target
