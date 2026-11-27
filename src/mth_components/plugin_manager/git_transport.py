"""HTTPS-to-SSH fallback probes for self-hosted git repositories.

Ports ``packages/boot/plugin-manager/src/git-transport.ts``: known forges are
left to pnpm's own fallback; other HTTP(S) repositories are probed read-only
over both transports and SSH is used only when HTTPS cannot read them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlparse

from magitech_harness.subprocess.service import child_env, scrubbed_parent_env

from mth_components.plugin_manager.operations import _kill, _spawn

DEFAULT_TIMEOUT_MS = 5_000
KNOWN_FORGE_HOSTS = (
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "gist.github.com",
)


def ssh_git_spec(spec: Mapping[str, Any]) -> str | None:
    """The SSH form of an HTTP(S) git spec on an unknown forge."""
    if spec.get("kind") != "git":
        return None
    value = str(spec["spec"])
    if not value.startswith(("https://", "http://", "git+https://", "git+http://")):
        return None
    parsed = urlparse(value.replace("git+", "", 1))
    host = (parsed.hostname or "").lower()
    if any(host == forge or host.endswith(f".{forge}") for forge in KNOWN_FORGE_HOSTS):
        return None
    if parsed.username or parsed.password or parsed.query:
        return None
    fragment = value[value.index("#") :] if "#" in value else ""
    return f"git+ssh://git@{host}{parsed.path}{fragment}"


async def readable(
    repository: str,
    root: str,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    env: Mapping[str, str] | None = None,
    spawn: Callable[..., Awaitable[Any]] | None = None,
    signal: Any = None,
) -> bool:
    """Whether Git can list the repository's HEAD without prompting."""
    fixed = {
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "SSH_ASKPASS_REQUIRE": "never",
    }
    environment = child_env({**fixed, **dict(env or {})}, scrubbed_parent_env())
    argv = [
        "git",
        "-c",
        "core.askPass=",
        "ls-remote",
        "--exit-code",
        "--",
        repository,
        "HEAD",
    ]
    try:
        process = await (spawn or _spawn)(argv, root, environment, signal=signal)
        await asyncio.wait_for(process.wait(), timeout=timeout_ms / 1000)
        return bool(process.returncode == 0)
    except TimeoutError:
        _kill(process)
        return False
    except OSError:
        return False


async def choose_git_transport(
    spec: Mapping[str, Any],
    root: str,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    env: Mapping[str, str] | None = None,
    spawn: Callable[..., Awaitable[Any]] | None = None,
    signal: Any = None,
) -> str:
    """Return the spec unchanged, or its SSH form when only SSH can read it."""
    ssh = ssh_git_spec(spec)
    if ssh is None:
        return str(spec["spec"])
    original = str(spec["spec"])

    def strip(value: str) -> str:
        return value.replace("git+", "", 1).split("#", 1)[0]

    if await readable(
        strip(original),
        root,
        timeout_ms=timeout_ms,
        env=env,
        spawn=spawn,
        signal=signal,
    ):
        return original
    if signal is not None and getattr(signal, "aborted", False):
        return original
    if await readable(
        strip(ssh), root, timeout_ms=timeout_ms, env=env, spawn=spawn, signal=signal
    ):
        return ssh
    return original


__all__ = ["choose_git_transport", "readable", "ssh_git_spec"]
