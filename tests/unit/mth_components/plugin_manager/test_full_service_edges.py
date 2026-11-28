"""Branch probes for the fully ported plugin-manager service."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from animas import Context

from mth_components.plugin_manager import github_connection, operations
from mth_components.plugin_manager.service import PluginManager
from mth_components.plugin_manager.types import RemoteError


def _seed(root: Path, name: str = "demo", version: str = "1.0.0") -> None:
    (root / "package.json").write_text(
        json.dumps(
            {"dependencies": {name: version}, "dsh": {"profile": {"bundles": [name]}}}
        ),
        encoding="utf-8",
    )
    package = root / "installed" / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "package.json").write_text(
        json.dumps({"name": name, "version": version, "dsh": {"bundle": {}}}),
        encoding="utf-8",
    )


def test_config_and_guard(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        PluginManager(Context(), {"root": str(tmp_path), "timeoutMs": 0})
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    manager._dispose()
    with pytest.raises(Exception, match="disposed"):
        manager._guard()


@pytest.mark.asyncio
async def test_set_bundle_incompatible(tmp_path: Path) -> None:
    _seed(tmp_path)
    manifest = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    manifest["dsh"]["profile"]["bundles"] = []
    (tmp_path / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "installed" / "demo" / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "version": "1.0.0",
                "engines": {"dsh": "^9.0.0"},
                "dsh": {"bundle": {}},
            }
        ),
        encoding="utf-8",
    )
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    result = await manager.set_bundle_enabled("demo", True)
    assert result["error"]["code"] == "incompatible-version"


@pytest.mark.asyncio
async def test_install_special_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    manager._installs["abort"] = {"aborted": True, "phase": "installing"}
    result = await manager.install_bundle("demo@1.0.0", {"requestId": "abort"})
    assert result["application"] == "cancelled"

    (tmp_path / "pnpm-workspace.yaml").write_text(
        "allowBuilds:\n  demo: set this to true or false\n", encoding="utf-8"
    )

    async def ok_run(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {
            "exitCode": 0,
            "output": "",
            "truncated": False,
            "logPath": "x",
            "timedOut": False,
        }

    monkeypatch.setattr(operations, "run_profile_pnpm", ok_run)
    monkeypatch.setattr(
        PluginManager,
        "registries",
        lambda self: asyncio.sleep(
            0,
            result={
                "registry": "https://registry.npmjs.org/",
                "fallbackRegistries": [],
                "resolved": None,
            },
        ),
    )
    result2 = await manager.install_bundle("demo@1.0.0", {"approvedBuilds": ["demo"]})
    assert result2["approvedBuilds"] == ["demo"]

    async def network_failure(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {
            "argv": ["git", "ls-remote"],
            "exitCode": 1,
            "output": "ENOTFOUND github.com",
            "truncated": False,
            "logPath": "x",
            "kind": "network",
            "timedOut": False,
        }

    monkeypatch.setattr(github_connection, "check_github_connection", network_failure)
    result3 = await manager.install_bundle("github:u/r#a" * 1 + "a" * 39)
    assert result3["failedAt"] == "spec-host"


@pytest.mark.asyncio
async def test_install_registry_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})

    async def no_registry(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"registry": None, "fallbackRegistries": [], "resolved": None}

    monkeypatch.setattr(PluginManager, "registries", no_registry)

    async def failed_run(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {
            "exitCode": 1,
            "output": "ERR_PNPM_FETCH_404",
            "truncated": False,
            "logPath": "x",
            "timedOut": False,
        }

    monkeypatch.setattr(operations, "run_profile_pnpm", failed_run)
    result = await manager.install_bundle("demo@1.0.0")
    assert result["application"] == "failed"

    async def ok_run(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {
            "exitCode": 0,
            "output": "",
            "truncated": False,
            "logPath": "x",
            "timedOut": False,
        }

    monkeypatch.setattr(
        PluginManager,
        "registries",
        lambda self: asyncio.sleep(
            0,
            result={
                "registry": "https://registry.npmjs.org/",
                "fallbackRegistries": [],
                "resolved": None,
            },
        ),
    )
    monkeypatch.setattr(operations, "run_profile_pnpm", ok_run)
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"demo": "1.0.0"}}), encoding="utf-8"
    )
    (tmp_path / "node_modules" / "demo").mkdir(parents=True)
    (tmp_path / "node_modules" / "demo" / "package.json").write_text(
        json.dumps({"version": "1.0.0", "dsh": {"bundle": {}}}), encoding="utf-8"
    )
    result2 = await manager.install_bundle(
        "demo@1.0.0", {"expected": {"name": "demo", "version": "1.0.0"}}
    )
    assert result2["bundle"] == "demo"

    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"a": "1", "b": "2"}}), encoding="utf-8"
    )
    result3 = await manager.install_bundle("demo@1.0.0")
    assert result3["application"] == "failed"


@pytest.mark.asyncio
async def test_detail_versions_and_update_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    with pytest.raises(RemoteError, match="No such bundle"):
        await manager.read_bundle_detail("absent", "en")
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

    _seed(tmp_path)
    (tmp_path / "installed" / "demo" / "package.json").write_text(
        json.dumps({"name": "demo", "version": "1.0.0", "dsh": {"bundle": {}}}),
        encoding="utf-8",
    )
    refused = await manager.list_versions("demo")
    assert refused["status"] == "refused"

    # A registry recorded spec that answers no versions.
    (tmp_path / "package.json").write_text(
        json.dumps(
            {"dependencies": {"demo": "1.0.0"}, "dsh": {"profile": {"bundles": []}}}
        ),
        encoding="utf-8",
    )

    async def failed_view(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {
            "exitCode": 1,
            "stdout": "",
            "stderr": "ERR_PNPM_FETCH_404",
            "timedOut": False,
        }

    monkeypatch.setattr(operations, "view_profile_package", failed_view)
    refused2 = await manager.list_versions("demo")
    assert refused2["status"] == "refused"

    update = await manager.update_bundle("demo", "bad")
    assert update["step"] == "check"
