"""Reading an install spec before a package manager sees it.

Ports ``packages/boot/plugin-manager/src/install-spec.ts``: spec forms
(registry name, absolute path, git address, tarball), pinned git origins and
the spec a profile dependency records.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from mth_components.plugin_manager.types import (
    InvalidInstallSpecError,
    ParsedInstallSpec,
)

GIT_SHORTHAND = re.compile(r"^(?:github|gitlab|bitbucket|gist):", re.IGNORECASE)
GIT_URL = re.compile(r"^git(?:\+[a-z]+)?://|^git@[^:]+:", re.IGNORECASE)
HOSTED_REPOSITORY_URL = re.compile(
    r"^https?://[^/]+/[^/]+/[^/#]+(?:\.git)?(?:#.*)?$", re.IGNORECASE
)
TARBALL_SPEC = re.compile(r"\.(?:tgz|tar\.gz)(?:#.*)?$", re.IGNORECASE)
PACKAGE_NAME = re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$")
PACKAGE_NAME_MAX_LENGTH = 214
FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
NON_REGISTRY_VALUE = re.compile(
    r"^(?!(?:npm|jsr|workspace|catalog):)[a-z][a-z0-9+.-]*:|^[^@/:\s]+@[^:/\s]+:",
    re.IGNORECASE,
)
HTTP_USER_INFO = re.compile(r"^((?:git\+)?https?://)[^/]*@", re.IGNORECASE)
FULL_COMMIT_FRAGMENT = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)

_GIT_SHORTHAND_HOSTS = {
    "github": "github.com",
    "gitlab": "gitlab.com",
    "bitbucket": "bitbucket.org",
    "gist": "gist.github.com",
}


def _invalid(spec: str, reason: str) -> InvalidInstallSpecError:
    return InvalidInstallSpecError(spec, reason)


def _git_host(spec: str) -> str:
    shorthand = re.match(r"^([a-z]+):", spec, re.IGNORECASE)
    if shorthand is not None:
        key = shorthand.group(1).lower()
        if key in _GIT_SHORTHAND_HOSTS:
            return _GIT_SHORTHAND_HOSTS[key]
    scp = re.match(r"^git@([^:]+):", spec, re.IGNORECASE)
    if scp is not None:
        return scp.group(1).lower()
    return urlparse(re.sub(r"^git\+", "", spec, flags=re.IGNORECASE)).netloc


def parse_install_spec(raw: str) -> ParsedInstallSpec:
    """Read one typed spec; refuse anything a registry or pnpm would refuse."""
    spec = raw.strip()
    if spec == "":
        raise _invalid(spec, "the package spec must not be empty")
    local = re.sub(r"^(?:file|link):", "", spec)
    if local != spec or Path(local).is_absolute():
        if not Path(local).is_absolute():
            raise _invalid(spec, "a local path must be absolute")
        if TARBALL_SPEC.search(local):
            return {"kind": "tarball", "spec": spec, "path": local}
        return {"kind": "path", "spec": spec, "path": local}
    if re.match(r"^\.{1,2}(?:[\\/]|$)", spec):
        raise _invalid(spec, "a local path must be absolute")
    git = bool(
        GIT_SHORTHAND.match(spec)
        or GIT_URL.match(spec)
        or HOSTED_REPOSITORY_URL.match(spec)
    )
    if git and not TARBALL_SPEC.search(spec):
        return {"kind": "git", "spec": spec, "host": _git_host(spec)}
    if re.match(r"^https?://", spec, re.IGNORECASE):
        if TARBALL_SPEC.search(spec):
            return {
                "kind": "tarball",
                "spec": spec,
                "host": urlparse(spec).netloc,
            }
        raise _invalid(spec, "a URL must point at a git repository or a tarball")
    at = spec.find("@", 1)
    name = spec if at == -1 else spec[:at]
    range_part = None if at == -1 else spec[at + 1 :]
    if len(name) > PACKAGE_NAME_MAX_LENGTH or PACKAGE_NAME.fullmatch(name) is None:
        raise _invalid(spec, "not a package name the registry accepts")
    if range_part == "":
        raise _invalid(spec, "a version after @ must not be empty")
    result: ParsedInstallSpec = {"kind": "registry", "spec": spec, "name": name}
    if range_part is not None:
        result["range"] = range_part
    return result


def pinned_git_origin(spec: str) -> str | None:
    """The repository origin of a git spec pinned to one full commit."""
    try:
        parsed = parse_install_spec(spec)
    except InvalidInstallSpecError:
        return None
    index = parsed["spec"].find("#")
    if parsed["kind"] != "git" or index == -1:
        return None
    parameters = parsed["spec"][index + 1 :].split("&")
    paths = [part for part in parameters if part.startswith("path:")]
    others = [part for part in parameters if not part.startswith("path:")]
    if (
        len(paths) > 1
        or len(others) != 1
        or FULL_COMMIT_FRAGMENT.fullmatch("".join(others)) is None
    ):
        return None
    path = (paths[0][len("path:") :] if paths else "").strip("/")
    location = parsed["spec"][:index]

    def shorthand(match: re.Match[str]) -> str:
        return f"{_GIT_SHORTHAND_HOSTS[match.group(1).lower()]}/"

    location = re.sub(
        r"^(github|gitlab|bitbucket|gist):", shorthand, location, flags=re.IGNORECASE
    )
    location = re.sub(r"^git\+", "", location, flags=re.IGNORECASE)
    location = re.sub(r"^[a-z][a-z0-9+.-]*://", "", location, flags=re.IGNORECASE)
    location = re.sub(r"^[^@/]+@", "", location)
    location = re.sub(r"^([^/:]+):(?!\d)", r"\1/", location)
    location = re.sub(r"/+$", "", location)
    location = re.sub(r"\.git$", "", location, flags=re.IGNORECASE)
    location = re.sub(r"^[^/]+", lambda match: match.group(0).lower(), location)
    return f"{location}#{path}"


def dependency_spec(
    name: str, recorded: str, profile_dir: str, package_name: str | None = None
) -> str:
    """The spec ``pnpm add`` accepts for a recorded profile dependency."""
    target = package_name or name
    local = re.match(r"^(file|link):(.*)$", recorded, re.DOTALL)
    if local is not None:
        path = re.sub(r"^~(?=$|[\\/])", str(Path("~").expanduser()), local.group(2))
        spec = f"{local.group(1)}:{Path(profile_dir, path).resolve()}"
    elif NON_REGISTRY_VALUE.search(recorded):
        spec = HTTP_USER_INFO.sub(r"\1", recorded)
    else:
        return f"{name}@{recorded}"
    return spec if target == name else f"{name}@{spec}"


__all__ = [
    "dependency_spec",
    "parse_install_spec",
    "pinned_git_origin",
]
