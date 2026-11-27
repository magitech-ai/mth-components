"""Last branch probes for the fully ported service."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from animas import Context

from mth_components.plugin_manager import operations
from mth_components.plugin_manager.service import PluginManager


@pytest.mark.asyncio
async def test_disposed_and_cancel_paths(tmp_path: Path) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    manager._dispose()
    with pytest.raises(Exception, match="disposed"):
        await manager.list_bundles()

    manager2 = PluginManager(Context(), {"root": str(tmp_path)})
    manager2._installs["cancel"] = {"aborted": True, "phase": "installing"}
    manager2._installs["cancel-loop"] = {"aborted": True, "phase": "installing"}

    class AbortOnce:
        def __init__(self) -> None:
            self.count = 0

        def __call__(self, *_a: Any, **_k: Any) -> Any:
            self.count += 1
            return asyncio.sleep(
                0, result={"registry": None, "fallbackRegistries": [], "resolved": None}
            )

    # Cancellation in the registry loop via a pre-aborted control.
    result = await manager2.install_bundle("demo@1.0.0", {"requestId": "cancel-loop"})
    assert result["application"] == "cancelled"


@pytest.mark.asyncio
async def test_install_error_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})

    async def ok(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {
            "exitCode": 0,
            "output": "",
            "truncated": False,
            "logPath": "x",
            "timedOut": False,
        }

    monkeypatch.setattr(operations, "run_profile_pnpm", ok)
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
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"demo": "1.0.0"}}), encoding="utf-8"
    )
    (tmp_path / "node_modules" / "demo").mkdir(parents=True)
    (tmp_path / "node_modules" / "demo" / "package.json").write_text(
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
    # Expected name missing from dependencies.
    missing = await manager.install_bundle(
        "demo@1.0.0", {"expected": {"name": "other", "version": "1.0.0"}}
    )
    assert missing["application"] == "failed"
    # Ambiguous dependency change.
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"a": "1", "b": "2"}}), encoding="utf-8"
    )
    ambiguous = await manager.install_bundle("demo@1.0.0")
    assert ambiguous["application"] == "failed"


@pytest.mark.asyncio
async def test_list_versions_and_update_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"demo": "1.0.0"},
                "dsh": {"profile": {"bundles": []}},
            }
        ),
        encoding="utf-8",
    )
    package = tmp_path / "installed" / "demo"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"name": "demo", "version": "1.0.0", "dsh": {"bundle": {}}}),
        encoding="utf-8",
    )
    manager = PluginManager(Context(), {"root": str(tmp_path)})

    answers = {
        "list": {
            "exitCode": 0,
            "stdout": "",
            "stderr": "",
            "timedOut": False,
            "answer": {"versions": ["1.1.0", "2.0.0"], "time": {}, "dist-tags": {}},
        },
        "detail": {
            "exitCode": 0,
            "stdout": "",
            "stderr": "",
            "timedOut": False,
            "answer": {"name": "demo", "version": "2.0.0", "dsh": {"bundle": {}}},
        },
        "bad": {
            "exitCode": 1,
            "stdout": "",
            "stderr": "ERR_PNPM_FETCH_404",
            "timedOut": False,
        },
    }

    async def view(spec: str, *_a: Any, **_k: Any) -> dict[str, Any]:
        return answers["list"] if spec == "demo" else answers["detail"]

    monkeypatch.setattr(operations, "view_profile_package", view)
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
    listed = await manager.list_versions("demo")
    assert listed["status"] == "listed" and listed["versions"]

    async def bad_view(*_a: Any, **_k: Any) -> dict[str, Any]:
        return answers["bad"]

    monkeypatch.setattr(operations, "view_profile_package", bad_view)
    refused = await manager.list_versions("demo")
    assert refused["status"] == "refused"

    monkeypatch.setattr(operations, "view_profile_package", view)

    async def install_ok(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {
            "changed": True,
            "application": "restart-required",
            "stage": "install",
            "target": "demo",
        }

    monkeypatch.setattr(manager, "install_bundle", install_ok)
    update = await manager.update_bundle(
        "demo", "2.0.0", {"registry": "https://registry.npmjs.org/"}
    )
    assert update["step"] == "install" and update["to"] == "2.0.0"
