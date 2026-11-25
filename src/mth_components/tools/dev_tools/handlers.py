"""Handlers for module reload and mapped-repo resolution."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from mth_components.tools.dev_tools.repo_map import (
    RepoEntry,
    parse_repo_map,
    resolve_repo,
)

__all__ = [
    "RepoEntry",
    "find_home",
    "handle_add_repo",
    "handle_reload_module",
    "parse_repo_map",
    "resolve_repo",
]

# --- module reload with current-turn read tracking -------------------------

_module_stats: dict[str, tuple[float, int]] = {}
_READABLE_TOOLS = frozenset({"read_file", "write_file"})
_HOME_NAME = "agents-home"


def _normalize(path: str) -> str:
    return str(Path(path).resolve())


def _current_turn_read_paths(run_ctx: Any) -> set[str]:
    session = None
    if isinstance(run_ctx, dict):
        session = run_ctx.get("session")
    if session is None:
        return set()
    events = getattr(session, "events", None) or []
    last_start = -1
    for index, event in enumerate(events):
        if str(getattr(event, "type", "")) == "turn/start":
            last_start = index
    paths: set[str] = set()
    for event in events[last_start:]:
        if str(getattr(event, "type", "")) != "tool/call":
            continue
        payload = getattr(event, "payload", None) or {}
        if payload.get("name") not in _READABLE_TOOLS:
            continue
        args = payload.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                continue
        if isinstance(args, dict) and isinstance(args.get("path"), str):
            paths.add(_normalize(args["path"]))
    return paths


def handle_reload_module(args: dict[str, Any], run_ctx: Any = None) -> str:
    module_name = args["module_name"]
    file_path = args["file_path"]
    if _normalize(file_path) not in _current_turn_read_paths(run_ctx):
        return (
            f"Refused: '{file_path}' has not been read yet. "
            f"Call read_file on '{file_path}' first, then retry reload_module."
        )
    path = Path(file_path)
    if not path.is_file():
        return f"Error: file not found: {file_path}"
    if path.suffix != ".py":
        path = path.with_suffix(".py")
        if not path.is_file():
            return f"Error: file not found: {file_path}"
    stat = path.stat()
    current = (stat.st_mtime, stat.st_size)
    cached = _module_stats.get(module_name)
    if cached is not None and cached == current:
        return (
            f"No changes detected for module '{module_name}' "
            f"since last reload (mtime={cached[0]:.1f})."
        )
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return f"Error: could not create spec for {path}."
    with contextlib.suppress(ValueError, OSError):
        Path(importlib.util.cache_from_source(str(path))).unlink(missing_ok=True)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _module_stats[module_name] = current
    return f"Reloaded module '{module_name}' from {path} (mtime={current[0]:.1f})."


# --- mapped repo resolution -------------------------------------------------


def _looks_like_home(path: Path) -> bool:
    return (
        path.is_dir() and (path / "AGENTS.md").is_file() and (path / "skills").is_dir()
    )


def find_home(home_dir: str | None = None) -> Path | None:
    override = home_dir or os.environ.get("AGENTS_HOME_PATH")
    if override:
        candidate = Path(override).expanduser()
        if _looks_like_home(candidate):
            return candidate.resolve()
    current = Path.cwd().resolve()
    for directory in (current, *current.parents):
        if directory.name == _HOME_NAME and _looks_like_home(directory):
            return directory  # pragma: no cover
        sibling = directory / _HOME_NAME
        if _looks_like_home(sibling):
            return sibling.resolve()  # pragma: no cover
    default = Path.home() / ".agents" / _HOME_NAME
    return default.resolve() if _looks_like_home(default) else None


def handle_add_repo(args: dict[str, Any], home_dir: str | None = None) -> str:
    home = find_home(home_dir)
    if home is None:
        return "Error: no agents-home checkout configured."
    ref = args["ref"]
    confirm = bool(args.get("confirm", False))
    agents_md = home / "AGENTS.md"
    if not agents_md.is_file():
        return "Error: home AGENTS.md not found."
    entry = resolve_repo(parse_repo_map(agents_md.read_text(encoding="utf-8")), ref)
    if entry is None:
        return f"Error: '{ref}' is not in the home repository map."
    target = (home / entry.path).resolve()
    workspace_root = home.resolve().parent
    if not target.is_relative_to(workspace_root):
        return f"Error: '{entry.name}' resolves outside the workspace root."
    if target.is_dir():
        return f"Resolved '{entry.name}' to {target}."
    if not entry.clone_url:
        return f"Error: '{entry.name}' has no clone URL in the map."
    if not confirm:
        return (
            f"Confirm with confirm=true to clone '{entry.name}' from "
            f"{entry.clone_url} into {target}."
        )
    result = subprocess.run(
        ["git", "clone", entry.clone_url, str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return f"Error: clone failed: {result.stderr.strip()[:300]}"
    return f"Cloned '{entry.name}' into {target}."
