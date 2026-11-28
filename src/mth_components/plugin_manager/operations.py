"""Package-manager operations for profile plugins.

Ports the service-side of ``packages/boot/plugin-manager/src/operations.ts``:
a bounded, cancellable pnpm runner with scrubbed environment, process-group
kill, diagnostic logs, expected-package validation and snapshot rollback, plus
the registry view helpers used by version lookups.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from magitech_harness.subprocess.service import child_env, scrubbed_parent_env

from mth_components.plugin_manager.install_failure import classify_install_failure
from mth_components.plugin_manager.types import PackageResultDict

DEFAULT_COMMAND = "pnpm"
DEFAULT_OUTPUT_BYTES = 16_384
DEFAULT_IDLE_TIMEOUT_MS = 600_000
DEFAULT_TIMEOUT_MS = 20_000
DEFAULT_CANCEL_POLL_MS = 10
REGISTRY_URL = re.compile(r"^https?://\S+$")


def registry_arguments(registry: str | None) -> list[str]:
    return [] if registry is None else [f"--registry={registry}"]


def anchor_path_spec(spec: str, cwd: str) -> str:
    """Anchor a relative local spec to the invocation directory."""
    prefix = ""
    value = spec
    for candidate in ("file:", "link:"):
        if value.startswith(candidate):
            prefix, value = candidate, value[len(candidate) :]
            break
    if value.startswith("./") or value.startswith("../"):
        return f"{prefix}{Path(cwd, value).resolve()}"
    return spec


def named_specs(args: Sequence[str]) -> list[str]:
    """Install specs following an add/install command, excluding flags."""
    result: list[str] = []
    installing = False
    for arg in args:
        if arg in ("add", "install", "i"):
            installing = True
            continue
        if installing and not arg.startswith("-") and arg != "--":
            result.append(arg)
    return result


def direct_dependencies(manifest: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in ("devDependencies", "dependencies", "optionalDependencies"):
        section = manifest.get(key)
        if isinstance(section, dict):
            result.update({str(name): str(value) for name, value in section.items()})
    return result


def save_manifest(root: str, manifest: Mapping[str, Any]) -> None:
    """Atomically write a profile manifest with private permissions."""
    path = Path(root, "package.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _tail(data: str, limit: int) -> tuple[str, bool]:
    if len(data.encode("utf-8")) <= limit:
        return data, False
    encoded = data.encode("utf-8")[-limit:]
    return encoded.decode("utf-8", errors="ignore"), True


async def _capture(
    stream: Any, sink: list[str], on_output: Callable[[str], None] | None
) -> None:
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        text = chunk.decode("utf-8", errors="replace")
        sink.append(text)
        if on_output is not None:
            on_output(text)


def _result(
    exit_code: int,
    output: str,
    truncated: bool,
    log_path: str,
    *,
    timed_out: bool = False,
    cause: BaseException | None = None,
) -> PackageResultDict:
    result: PackageResultDict = {
        "exitCode": exit_code if not timed_out else 1,
        "output": output,
        "truncated": truncated,
        "logPath": log_path,
        "timedOut": timed_out,
    }
    if exit_code != 0 or timed_out:
        result["kind"] = classify_install_failure(output, cause, timed_out)
    return result


async def run_profile_pnpm(
    root: str,
    args: Sequence[str],
    *,
    command: str = DEFAULT_COMMAND,
    env: Mapping[str, str] | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    idle_timeout_ms: int = DEFAULT_IDLE_TIMEOUT_MS,
    output_bytes: int = DEFAULT_OUTPUT_BYTES,
    cancel_poll_ms: int = DEFAULT_CANCEL_POLL_MS,
    on_output: Callable[[str], None] | None = None,
    expected: Mapping[str, str] | None = None,
    registry: str | None = None,
    spawn: Callable[..., Awaitable[Any]] | None = None,
    signal: Any = None,
) -> PackageResultDict:
    """Run one pnpm operation with bounded output and snapshot rollback."""
    argv = [command, *args, *registry_arguments(registry)]
    environment = child_env(env, scrubbed_parent_env())
    logs = Path(root, ".plugin-manager", "logs", f"operation-{uuid.uuid4()}")
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = logs / "pnpm.log"
    with log_path.open("x", encoding="utf-8") as _log:
        pass
    log_path.chmod(0o600)
    manifest_path = Path(root, "package.json")
    lock_path = Path(root, "pnpm-lock.yaml")
    before_text = _read_text(manifest_path)
    before_lock = _read_text(lock_path)
    stdout: list[str] = []
    stderr: list[str] = []
    try:
        process = await (spawn or _spawn)(argv, root, environment, signal=signal)
    except OSError as error:
        output, truncated = _tail(str(error), output_bytes)
        return _result(127, output, truncated, str(log_path), cause=error)
    tasks = [
        asyncio.create_task(_capture(process.stdout, stdout, on_output)),
        asyncio.create_task(_capture(process.stderr, stderr, on_output)),
    ]
    watcher: asyncio.Task[None] | None = None
    if signal is not None:
        watcher = asyncio.create_task(
            _watch_cancel(process, signal, cancel_poll_ms / 1000)
        )
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=idle_timeout_ms / 1000)
    except TimeoutError:
        timed_out = True
        _kill(process)
        await process.wait()
    await asyncio.gather(*tasks)
    if watcher is not None:
        watcher.cancel()
    output, truncated = _tail("".join(stdout) + "".join(stderr), output_bytes)
    log_path.write_text("".join(stdout) + "".join(stderr), encoding="utf-8")
    if timed_out or (signal is not None and getattr(signal, "aborted", False)):
        _restore(manifest_path, before_text)
        _restore(lock_path, before_lock)
        return _result(1, output, truncated, str(log_path), timed_out=True)
    code = process.returncode or 0
    result = _result(code, output, truncated, str(log_path))
    if code == 0 and expected is not None:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        problem = expected_unmet(manifest, root, expected)
        if problem is not None:
            _restore(manifest_path, before_text)
            _restore(lock_path, before_lock)
            result["exitCode"] = 1
            result["kind"] = problem
            result["output"] = f"{output}\n{problem}: expected package check failed"
    return result


async def _watch_cancel(
    process: Any, signal: Any, poll_seconds: float = DEFAULT_CANCEL_POLL_MS / 1000
) -> None:
    """Kill a running process tree when the caller aborts."""
    while not getattr(signal, "aborted", False):
        await asyncio.sleep(poll_seconds)
    _kill(process)


async def _spawn(
    argv: Sequence[str], cwd: str, env: Mapping[str, str], signal: Any = None
) -> Any:
    del signal
    return await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=dict(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name != "nt",
    )


def _kill(process: Any) -> None:
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, 9)
    except (OSError, AttributeError):
        process.kill()


def _restore(path: Path, before: str | None) -> None:
    if before is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(before, encoding="utf-8")


def expected_unmet(
    manifest: Mapping[str, Any], root: str, expected: Mapping[str, str]
) -> str | None:
    """Check the expected package, version and bundle after an install."""
    name, version = expected.get("name"), expected.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        return "invalid-spec"
    dependencies = direct_dependencies(manifest)
    recorded = dependencies.get(name)
    if recorded != version and (
        recorded is None or not recorded.endswith(f"@{version}")
    ):
        return "unexpected-package"
    package_dir = Path(root, "node_modules", name, "package.json")
    try:
        installed = json.loads(package_dir.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unexpected-package"
    if installed.get("version") != version:
        return "unexpected-package"
    dsh = installed.get("dsh")
    if not isinstance(dsh, dict) or not isinstance(dsh.get("bundle"), dict):
        return "not-bundle"
    return None


async def view_profile_package(  # noqa: PLR0917
    spec: str,
    fields: Sequence[str],
    root: str,
    registry: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    command: str = DEFAULT_COMMAND,
    spawn: Callable[..., Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    """Run one bounded ``pnpm view`` lookup and return its answer."""
    argv = [
        command,
        "view",
        spec,
        *fields,
        "--json",
        *registry_arguments(registry),
        "--config.fetch-retries=0",
    ]
    environment = child_env(None, scrubbed_parent_env())
    process = await (spawn or _spawn)(argv, root, environment)
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_ms / 1000
        )
        code, timed_out = process.returncode, False
    except TimeoutError:
        _kill(process)
        await process.wait()
        stdout, stderr, code, timed_out = b"", b"", 1, True
    text = (stdout or b"").decode("utf-8", errors="replace")
    answer: dict[str, Any] = {
        "exitCode": None if timed_out else code,
        "stdout": text,
        "stderr": (stderr or b"").decode("utf-8", errors="replace"),
        "timedOut": timed_out,
    }
    if not timed_out and code == 0:
        try:
            parsed = json.loads(text)
            answer["answer"] = (
                parsed[-1] if isinstance(parsed, list) and parsed else parsed
            )
        except ValueError:
            answer["answer"] = None
    return answer


async def read_profile_registry(
    root: str,
    command: str = DEFAULT_COMMAND,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    spawn: Callable[..., Awaitable[Any]] | None = None,
) -> str | None:
    """Read the registry pnpm itself names, or ``None`` when unreadable."""
    environment = child_env(None, scrubbed_parent_env())
    try:
        process = await (spawn or _spawn)(
            [command, "config", "get", "registry"], root, environment
        )
    except OSError:
        return None
    try:
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_ms / 1000
        )
    except TimeoutError:
        _kill(process)
        return None
    lines = [
        line.strip()
        for line in (stdout or b"").decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    value = lines[-1] if lines else ""
    return value if REGISTRY_URL.fullmatch(value) else None


__all__ = [
    "DEFAULT_CANCEL_POLL_MS",
    "DEFAULT_COMMAND",
    "DEFAULT_IDLE_TIMEOUT_MS",
    "DEFAULT_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_MS",
    "anchor_path_spec",
    "direct_dependencies",
    "expected_unmet",
    "named_specs",
    "read_profile_registry",
    "registry_arguments",
    "run_profile_pnpm",
    "save_manifest",
    "view_profile_package",
]
