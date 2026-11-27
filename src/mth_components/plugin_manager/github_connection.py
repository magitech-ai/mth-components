"""Pre-install GitHub repository access check.

Ports ``packages/boot/plugin-manager/src/github-connection.ts``: a bounded,
credential-scrubbed ``git ls-remote`` that only stops an install on network or
timeout failures; other failures are left for pnpm to report.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from magitech_harness.subprocess.service import child_env, scrubbed_parent_env

from mth_components.plugin_manager.install_failure import classify_install_failure
from mth_components.plugin_manager.install_spec import HTTP_USER_INFO
from mth_components.plugin_manager.operations import _kill, _spawn, _tail

DEFAULT_TIMEOUT_MS = 5_000
DEFAULT_OUTPUT_BYTES = 16_384


def is_github_spec(spec: Mapping[str, Any]) -> bool:
    """Whether a git spec names GitHub and needs the access probe."""
    if spec.get("kind") != "git":
        return False
    host = str(spec.get("host", "")).split(":", 1)[0].lower()
    return host == "github.com" or host.endswith(".github.com")


def repository_url(spec: Mapping[str, Any]) -> str:
    """Normalize a spec to the repository URL Git reads."""
    value = str(spec["spec"]).split("#", 1)[0]
    for shorthand, host in (
        ("github", "github.com"),
        ("gitlab", "gitlab.com"),
        ("bitbucket", "bitbucket.org"),
        ("gist", "gist.github.com"),
    ):
        if value.startswith(f"{shorthand}:"):
            value = f"https://{host}/{value.split(':', 1)[1]}"
            break
    return value.replace("git+", "", 1) if value.startswith("git+") else value


def scrubbed_argv(repository: str) -> list[str]:
    """The probe argv with URL credentials removed for display."""
    argv = ["git", "-c", "credential.helper=", "ls-remote", "--", repository, "HEAD"]
    return [HTTP_USER_INFO.sub(r"\1", part) for part in argv]


async def check_github_connection(
    spec: Mapping[str, Any],
    root: str,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    output_bytes: int = DEFAULT_OUTPUT_BYTES,
    env: Mapping[str, str] | None = None,
    spawn: Callable[..., Awaitable[Any]] | None = None,
    signal: Any = None,
) -> dict[str, Any] | None:
    """Return a failure record when GitHub cannot be reached, else ``None``."""
    if not is_github_spec(spec):
        return None
    repository = repository_url(spec)
    logs = Path(root, ".plugin-manager", "logs", f"github-connection-{uuid.uuid4()}")
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = logs / "git.log"
    fixed = {
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "SSH_ASKPASS_REQUIRE": "never",
    }
    environment = child_env({**fixed, **dict(env or {})}, scrubbed_parent_env())
    argv = ["git", "-c", "credential.helper=", "ls-remote", "--", repository, "HEAD"]
    timed_out = False
    with log_path.open("x", encoding="utf-8") as handle:
        log_path.chmod(0o600)
        try:
            process = await (spawn or _spawn)(argv, root, environment, signal=signal)
            try:
                _stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout_ms / 1000
                )
                code = process.returncode or 0
            except TimeoutError:
                timed_out = True
                _kill(process)
                await process.wait()
                code, stderr = 1, b""
            handle.write((stderr or b"").decode("utf-8", errors="replace"))
        except OSError as error:
            code, stderr = 127, str(error).encode()
            handle.write(str(error))
    if code == 0 and not timed_out:
        return None
    log = (stderr or b"").decode("utf-8", errors="replace")
    if timed_out:
        log += "\nGitHub connection check timed out"
    output, truncated = _tail(log, output_bytes)
    return {
        "argv": scrubbed_argv(repository),
        "cwd": root,
        "exitCode": 127 if code == 127 else 1,
        "output": output,
        "truncated": truncated,
        "logPath": str(log_path),
        "kind": classify_install_failure(log, timed_out=timed_out),
        "timedOut": timed_out,
    }


__all__ = [
    "check_github_connection",
    "is_github_spec",
    "repository_url",
    "scrubbed_argv",
]
