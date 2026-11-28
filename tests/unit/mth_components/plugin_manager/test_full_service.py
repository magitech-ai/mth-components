"""Probes for the fully ported plugin-manager service methods."""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any

import pytest
from animas import Context
from magitech_harness import __version__

from mth_components.plugin_manager import operations
from mth_components.plugin_manager.service import (
    CHANGED_EVENT,
    INSTALL_LOG,
    INSTALL_STATE,
    OPERATION_EVENT,
    PluginManager,
)


class FakeProcess:
    def __init__(self, stdout: bytes = b"{}", code: int = 0) -> None:
        self.stdout = _stream(stdout)
        self.stderr = _stream(b"")
        self.returncode = code
        self.pid = 1

    async def wait(self) -> int:
        return self.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout_bytes, b""

    def kill(self) -> None:
        return None

    @property
    def stdout_bytes(self) -> bytes:
        return b"{}"


def _stream(data: bytes) -> Any:
    async def make() -> Any:
        stream = asyncio.StreamReader()
        stream.feed_data(data)
        stream.feed_eof()
        return stream

    return asyncio.run(make())


def _manager(root: Path, **config: Any) -> PluginManager:
    return PluginManager(Context(), {"root": str(root), **config})


def _seed(root: Path, name: str = "demo", version: str = "1.0.0") -> None:
    (root / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {name: version},
                "dsh": {"profile": {"bundles": [name]}},
            }
        ),
        encoding="utf-8",
    )
    package = root / "installed" / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": version,
                "description": "test bundle",
                "dsh": {"bundle": {"rows": [{"id": "r", "module": name}]}},
            }
        ),
        encoding="utf-8",
    )
    (root / "node_modules" / name).mkdir(parents=True, exist_ok=True)
    (root / "node_modules" / name / "package.json").write_text(
        json.dumps({"version": version, "dsh": {"bundle": {}}}), encoding="utf-8"
    )
    (root / "plugin-patches.json").write_text(
        json.dumps(
            {"entries": {"r": {"moduleName": name, "bundle": name, "enabled": True}}}
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_registry_install_and_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    ctx = Context()
    manager = PluginManager(ctx, {"root": str(tmp_path)})
    ctx.on(OPERATION_EVENT, lambda *_a: events.append("operation"))
    ctx.on(CHANGED_EVENT, lambda *_a: events.append("changed"))
    ctx.on(INSTALL_STATE, lambda *_a: events.append("state"))
    ctx.on(INSTALL_LOG, lambda *_a: events.append("log"))

    async def run(*_a: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "exitCode": 0,
            "output": "",
            "truncated": False,
            "logPath": "x",
            "timedOut": False,
        }

    monkeypatch.setattr(operations, "run_profile_pnpm", run)
    monkeypatch.setattr(
        operations,
        "read_profile_registry",
        lambda *_a, **_k: asyncio.sleep(0, result="https://registry.npmjs.org/"),
    )
    result = await manager.install_bundle("demo@1.0.0")
    assert result["application"] == "failed"
    assert "operation" in events

    failed: dict[str, Any] = {
        "exitCode": 1,
        "output": "ERR_PNPM_META_FETCH_FAIL",
        "kind": "network",
        "truncated": False,
        "logPath": "x",
        "timedOut": False,
    }

    async def failing(*_a: Any, **_kwargs: Any) -> dict[str, Any]:
        return failed

    monkeypatch.setattr(operations, "run_profile_pnpm", failing)
    result2 = await manager.install_bundle("demo@1.0.0")
    assert result2["application"] == "failed" and result2["failedAt"] == "registry"
    assert "log" in events


@pytest.mark.asyncio
async def test_detail_versions_update_and_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path)
    manager = _manager(tmp_path, versionListLimit=2)
    monkeypatch.setattr(
        operations,
        "view_profile_package",
        lambda *_a, **_k: asyncio.sleep(
            0,
            result={
                "exitCode": 1,
                "stdout": "",
                "stderr": "ERR_PNPM_FETCH_404",
                "timedOut": False,
            },
        ),
    )
    monkeypatch.setattr(
        operations,
        "read_profile_registry",
        lambda *_a, **_k: asyncio.sleep(0, result=None),
    )
    assert (await manager.read_bundle_detail("demo", "en"))["status"] == "none"

    refused = await manager.list_versions("demo")
    assert refused["status"] == "refused"

    (tmp_path / "installed" / "demo" / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "version": "1.0.0",
                "meta": {"detail": "guide.md"},
                "dsh": {"bundle": {"rows": []}},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "installed" / "demo" / "guide.md").write_text(
        "# Guide", encoding="utf-8"
    )
    detail = await manager.read_bundle_detail("demo", "en")
    assert detail["status"] == "found"


@pytest.mark.asyncio
async def test_controls_and_remaining_methods(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    assert await manager.wait_for_install("unknown") is None
    assert (await manager.cancel_install("unknown"))["status"] == "not-running"
    manager._installs["active"] = {
        "phase": "installing",
        "aborted": False,
        "result": {"changed": False},
    }
    assert (await manager.cancel_install("active"))["status"] == "cancelled"
    manager._installs["active"]["phase"] = "applying"
    assert (await manager.cancel_install("active"))["status"] == "too-late"
    assert await manager.wait_for_install("active") is not None

    result = await manager.update_bundle("missing", "2.0.0")
    assert result["step"] == "check"


@pytest.mark.asyncio
async def test_compatibility_guards_and_hmr(tmp_path: Path) -> None:
    _seed(tmp_path)
    (tmp_path / "installed" / "demo" / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "version": "1.0.0",
                "engines": {"dsh": "^9.0.0"},
                "dsh": {"bundle": {"rows": []}},
            }
        ),
        encoding="utf-8",
    )
    manager = _manager(tmp_path)
    result = await manager.set_plugin_enabled("r", False)
    assert (
        result["application"] == "failed"
        and result["error"]["code"] == "incompatible-version"
    )
    await manager.set_version_exemption("demo@1.0.0", __version__, True, True)

    class Hmr:
        def __init__(self) -> None:
            self.called = False

        async def run_exclusive(self, job: Any) -> Any:
            self.called = True
            answer = job()
            return await answer if inspect.isawaitable(answer) else answer

    hmr = Hmr()
    ctx = Context()
    ctx.provide("hmr", hmr)
    manager2 = PluginManager(ctx, {"root": str(tmp_path / "other")})
    (tmp_path / "other").mkdir(exist_ok=True)
    await manager2._configure(lambda: asyncio.sleep(0, result=1))
    assert hmr.called
