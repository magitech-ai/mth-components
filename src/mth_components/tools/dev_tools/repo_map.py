"""Parse the curated repository map in the home ``AGENTS.md``.

The map is the home repo's source of truth for sibling checkout paths and
clone URLs. This is the single parser implementation: the ``add_repo`` tool
uses it directly, and home-swe-home re-exports it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_MARKER = "## Other Repos"
_NAME_RE = re.compile(r"^- \*\*(.+?)\*\*")
_PATH_RE = re.compile(r"\]\((\.{1,2}/[^)]+)\)|`(\.{1,2}/[^`]+)`")
_URL_RE = re.compile(r"<(https://github\.com/[^>]+)>")


@dataclass(frozen=True)
class RepoEntry:
    name: str
    path: str
    clone_url: str = ""


def parse_repo_map(text: str) -> list[RepoEntry]:
    lines = text.splitlines()
    if _MARKER not in lines:
        return []
    groups: list[list[str]] = []
    for line in lines[lines.index(_MARKER) + 1 :]:
        if line.startswith("## "):
            break
        if _NAME_RE.match(line):
            groups.append([line])
        elif groups and line.strip():
            groups[-1].append(line.strip())
    entries: list[RepoEntry] = []
    for group in groups:
        joined = " ".join(part.strip() for part in group)
        name = _NAME_RE.match(joined)
        path = _PATH_RE.search(joined)
        if name is None or path is None:
            continue
        url = _URL_RE.search(joined)
        entries.append(
            RepoEntry(
                name=name.group(1).strip(),
                path=(path.group(1) or path.group(2)).rstrip("/"),
                clone_url=url.group(1) if url else "",
            )
        )
    return entries


def resolve_repo(entries: list[RepoEntry], ref: str) -> RepoEntry | None:
    wanted = ref.strip().rstrip("/")
    for entry in entries:
        if entry.name.casefold() == wanted.casefold():
            return entry
    for entry in entries:
        if entry.clone_url and entry.clone_url.removesuffix(
            ".git"
        ) == wanted.removesuffix(".git"):
            return entry
    for entry in entries:
        if entry.path == wanted:
            return entry
    return None
