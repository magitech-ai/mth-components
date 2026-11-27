"""Build-script approval policy in ``pnpm-workspace.yaml``.

Ports ``packages/boot/plugin-manager/src/build-approval.ts``: pending build
names are recorded with a sentinel, blocked names are read from pnpm output,
and approval is only granted for names that are still pending. The editor is
line-preserving so existing comments and unrelated settings survive.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml  # type: ignore[import-untyped]

from mth_components.plugin_manager.types import ManagementFailure

UNDECIDED_BUILD = "set this to true or false"
WORKSPACE_FILE = "pnpm-workspace.yaml"
IGNORED_BUILDS = re.compile(r"\bIgnored build scripts: (.+)")
GIT_DEP_PREPARE = re.compile(
    r"ERR_PNPM_GIT_DEP_PREPARE_NOT_ALLOWED[\s\S]*?^allowBuilds:\r?\n[ \t]+(.+?): true$",
    re.MULTILINE,
)
HTTP_USER_INFO = re.compile(r"^((?:git\+)?https?://)[^/]*@", re.IGNORECASE)


def _path(root: str) -> Path:
    return Path(root, WORKSPACE_FILE)


def read_policy(root: str) -> dict[str, Any]:
    """Read the ``allowBuilds`` mapping, rejecting malformed or aliased YAML."""
    path = _path(root)
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        document = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError) as error:
        raise ManagementFailure(
            "operation-error", f"cannot read {WORKSPACE_FILE}: {error}"
        ) from error
    if not isinstance(document, dict):
        raise ManagementFailure(
            "operation-error", f"{WORKSPACE_FILE} must be a mapping"
        )
    policy = document.get("allowBuilds", {})
    if (
        not isinstance(policy, dict)
        or "&" in raw
        or "*" in raw.split("allowBuilds:", 1)[-1]
    ):
        raise ManagementFailure(
            "operation-error", "allowBuilds must be a plain mapping"
        )
    return {str(key): value for key, value in policy.items()}


def pending_builds(policy: Mapping[str, Any]) -> list[str]:
    """Names whose approval is still undecided."""
    return [
        name
        for name, value in policy.items()
        if value == UNDECIDED_BUILD and "*" not in name and "?" not in name
    ]


def _carries_credential(name: str) -> bool:
    if re.search(r"https?://[^/]*@", name) is not None:
        return True
    if "@git+" not in name:
        return False
    url = name.split("@git+", 1)[1]
    if url.startswith("http://") or url.startswith("https://"):
        url = f"git+{url}"
    url = HTTP_USER_INFO.sub(r"\1", url)
    try:
        parsed = urlparse(url.replace("git+", "", 1))
    except ValueError:
        return False
    return bool(
        parsed.password or (parsed.username and parsed.scheme in ("http", "https"))
    )


def blocked_build_names(output: str) -> list[str]:
    """Names pnpm reported as blocked by the build-script policy."""
    names: list[str] = []
    match = IGNORED_BUILDS.search(output)
    if match is not None:
        names.extend(part.strip() for part in match.group(1).split(","))
    prepare = GIT_DEP_PREPARE.search(output)
    if prepare is not None:
        names.append(prepare.group(1).strip())
    return [
        name
        for name in names
        if name
        and "*" not in name
        and "?" not in name
        and not _carries_credential(name)
    ]


def _set_entries(root: str, updates: Mapping[str, str]) -> None:
    """Set allowBuilds entries in place, preserving all other lines."""
    path = _path(root)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines(keepends=True)
    header = next(
        (
            index
            for index, line in enumerate(lines)
            if re.match(r"^allowBuilds:\s*(#.*)?$", line.rstrip("\r\n"))
        ),
        None,
    )
    if header is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append("allowBuilds:\n")
        header = len(lines) - 1
    end = len(lines)
    for index in range(header + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped and not stripped.startswith("#") and not lines[index][0].isspace():
            end = index
            break
    existing: dict[str, int] = {}
    indent = "  "
    for index in range(header + 1, end):
        match = re.match(r"^(\s+)([^:#]+):", lines[index])
        if match is not None:
            indent = match.group(1)
            existing[match.group(2).strip()] = index
    for name, value in updates.items():
        line = f"{indent}{name}: {value}\n"
        if name in existing:
            index = existing[name]
            comment = ""
            if "#" in lines[index]:
                comment = " #" + lines[index].split("#", 1)[1]
            lines[index] = f"{indent}{name}: {value}{comment}\n"
        else:
            lines.insert(end, line)
            end += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".yaml.tmp")
    temp.write_text("".join(lines), encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(path)


def record_pending_builds(root: str, output: str) -> list[str]:
    """Record newly blocked names with the undecided sentinel."""
    policy = read_policy(root)
    pending = pending_builds(policy)
    added = [name for name in blocked_build_names(output) if name not in policy]
    if added:
        _set_entries(root, dict.fromkeys(added, UNDECIDED_BUILD))
    return pending + added


def approve_builds(root: str, names: list[str]) -> None:
    """Grant approval for names that are still pending."""
    if not names:
        return
    policy = read_policy(root)
    pending = set(pending_builds(policy))
    stale = [name for name in names if name not in pending]
    if stale:
        raise ManagementFailure(
            "stale-approval", f"builds no longer pending: {', '.join(stale)}"
        )
    _set_entries(root, dict.fromkeys(names, "true"))


__all__ = [
    "UNDECIDED_BUILD",
    "WORKSPACE_FILE",
    "approve_builds",
    "blocked_build_names",
    "pending_builds",
    "read_policy",
    "record_pending_builds",
]
