"""Pure-Python ``grep`` tool: search file contents with a regular expression.

Python adaptation: the reference drives ripgrep ``--json`` via subprocess; this
port uses Python ``re`` and ``pathlib`` so the wheel stays self-contained and
cross-platform. Observable contract (regex content search, line numbers grouped
by file, match cap with spill note) matches the reference. Paths are confined to
the workspace root.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from mth_plugins.tools.fs.path_guard import PathEscapeError, confined_path
from mth_plugins.tools.fs_search.glob import _relative, _resolve_root

DEFAULT_MAX_MATCHES = 250
DEFAULT_MAX_LINE_BYTES = 2000
_VCS_DIRS = {".git", ".svn", ".hg", ".bzr", ".jj", ".sl"}


def _iter_files(root: Path, include: str | None) -> list[Path]:
    files: list[Path] = []
    for candidate in root.rglob("*"):
        try:
            if not candidate.is_file():
                continue
            if any(part in _VCS_DIRS for part in candidate.parts):
                continue
            if include and not _fnmatch_path(candidate, include):
                continue
            confined_path(root, str(candidate))
            files.append(candidate)
        except (
            PathEscapeError,
            OSError,
            ValueError,
        ):  # pragma: no cover - defensive fs error
            continue
    return files


def _fnmatch_path(path: Path, include: str) -> bool:
    if "/" in include:
        return fnmatch.fnmatch(str(path), f"*{include}")
    return fnmatch.fnmatch(path.name, include)


def handle_grep(  # noqa: PLR0912
    args: dict[str, Any],
    *,
    workspace_root: Path | str | None = None,
    max_matches: int = DEFAULT_MAX_MATCHES,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> str:
    """Search file contents by regex under the workspace root."""
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or pattern == "":
        return "Error: 'pattern' argument is required."
    include = args.get("include")
    if include is not None and (not isinstance(include, str) or not include.strip()):
        return "Error: 'include' argument must be a non-empty string."
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"Error: invalid regular expression: {exc}"

    root = _resolve_root(args.get("path"), workspace_root)
    files = [root] if root.is_file() else _iter_files(root, include)

    matches: list[tuple[str, int, str]] = []
    for file in files:
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - defensive fs read error
            continue
        rel = _relative(root, file).as_posix()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                preview = line
                if len(preview.encode("utf-8")) > max_line_bytes:
                    preview = preview[:max_line_bytes] + " (line truncated)"
                matches.append((rel, lineno, preview))

    if not matches:
        return "No matches found"

    kept = matches[:max_matches]
    by_file: dict[str, list[tuple[int, str]]] = {}
    for path, lineno, line in kept:
        by_file.setdefault(path, []).append((lineno, line))
    lines_out: list[str] = []
    for path, rows in by_file.items():
        lines_out.append(path)
        for lineno, line in rows:
            lines_out.append(f"  Line {lineno}: {line}")
    header = (
        f"Found {len(kept)} matches"
        if len(kept) == len(matches)
        else f"Found {len(kept)} of {len(matches)} matches"
    )
    body = "\n".join(lines_out)
    if len(kept) < len(matches):
        body += "\n(Full grep result was limited by maxMatches.)"
    return f"{header}\n\n{body}"
