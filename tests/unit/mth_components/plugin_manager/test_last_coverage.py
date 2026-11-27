"""Final coverage probes for the full plugin-manager port."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from animas import Context

import mth_components.plugin_manager.service as service_module
from mth_components.plugin_manager import official_install_target, operations
from mth_components.plugin_manager.service import (
    PluginManager,
    _AbortState,
    _restore_snapshot,
)
from mth_components.plugin_manager.types import InstallCancelledError, ManagementFailure


@pytest.mark.asyncio
async def test_sync_configure_and_registry_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    assert await manager._configure(lambda: 42) == 42

    monkeypatch.setattr(
        PluginManager,
        "registries",
        lambda self: asyncio.sleep(
            0,
            result={
                "registry": None,
                "fallbackRegistries": [],
                "resolved": None,
            },
        ),
    )
    monkeypatch.setattr(service_module, "registry_plan", lambda *_a, **_k: [])
    result = await manager.install_bundle("demo@1.0.0")
    assert result["application"] == "failed"


@pytest.mark.asyncio
async def test_cancel_during_install_and_incompatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})

    async def slow_local(*_a: Any, **_k: Any) -> tuple[str, str]:
        await asyncio.sleep(0.02)
        return "demo", "1.0.0"

    monkeypatch.setattr(manager, "_install_local_source", slow_local)
    task = asyncio.create_task(
        manager.install_bundle("/tmp/demo", {"requestId": "cancel-applying"})
    )
    await asyncio.sleep(0)
    await manager.cancel_install("cancel-applying")
    result = await task
    assert result["application"] == "cancelled"

    manager2 = PluginManager(Context(), {"root": str(tmp_path / "second")})
    (tmp_path / "second").mkdir(exist_ok=True)
    (tmp_path / "second" / "package.json").write_text(
        json.dumps({"dependencies": {"demo": "1.0.0"}}), encoding="utf-8"
    )
    installed = tmp_path / "second" / "installed" / "demo"
    installed.mkdir(parents=True)
    (installed / "package.json").write_text(
        json.dumps({"name": "demo", "version": "1.0.0", "engines": {"dsh": "^9.0.0"}}),
        encoding="utf-8",
    )
    node = tmp_path / "second" / "node_modules" / "demo"
    node.mkdir(parents=True)
    (node / "package.json").write_text(
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
    monkeypatch.setattr(
        operations,
        "run_profile_pnpm",
        lambda *_a, **_k: asyncio.sleep(
            0,
            result={
                "exitCode": 0,
                "output": "",
                "truncated": False,
                "logPath": "x",
                "timedOut": False,
            },
        ),
    )
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
    result2 = await manager2.install_bundle(
        "demo@1.0.0", {"expected": {"name": "demo", "version": "1.0.0"}}
    )
    assert result2["application"] == "failed"


@pytest.mark.asyncio
async def test_list_versions_detail_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"demo": "file:/tmp/demo"},
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
    assert (await manager.list_versions("demo"))["problem"] == "not-updatable"

    (tmp_path / "package.json").write_text(
        json.dumps(
            {"dependencies": {"demo": "1.0.0"}, "dsh": {"profile": {"bundles": []}}}
        ),
        encoding="utf-8",
    )
    views = iter(
        [
            {
                "exitCode": 0,
                "stdout": "",
                "stderr": "",
                "timedOut": False,
                "answer": {"versions": ["2.0.0"], "time": {}, "dist-tags": {}},
            },
            {"exitCode": 1, "stdout": "", "stderr": "ERR", "timedOut": False},
        ]
    )

    async def view(*_a: Any, **_k: Any) -> dict[str, Any]:
        return next(views)

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
    assert listed["status"] == "listed" and listed["versions"] == []


def test_installed_from_spec_candidate(tmp_path: Path) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"a": "1", "b": "2", "demo": "file:/tmp/demo"}}),
        encoding="utf-8",
    )
    name, version = manager._installed_from_spec(
        {},
        {},
        "file:/tmp/demo",
        {"kind": "path", "spec": "file:/tmp/demo"},
        {"beforeDependencies": {}},
    )
    assert name == "demo" and version == "file:/tmp/demo"


@pytest.mark.asyncio
async def test_cancel_in_registry_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})

    async def slow_pnpm(*_a: Any, **_k: Any) -> dict[str, Any]:
        await asyncio.sleep(0.02)
        return {
            "exitCode": 0,
            "output": "",
            "truncated": False,
            "logPath": "x",
            "timedOut": False,
        }

    monkeypatch.setattr(operations, "run_profile_pnpm", slow_pnpm)
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
    task = asyncio.create_task(
        manager.install_bundle("demo@1.0.0", {"requestId": "loop"})
    )
    await asyncio.sleep(0)
    await manager.cancel_install("loop")
    assert (await task)["application"] == "cancelled"


@pytest.mark.asyncio
async def test_last_version_and_update_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {"dependencies": {"demo": "1.0.0"}, "dsh": {"profile": {"bundles": []}}}
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
    answers = iter(
        [
            {
                "exitCode": 0,
                "stdout": "",
                "stderr": "",
                "timedOut": False,
                "answer": {"versions": ["2.0.0"], "time": {}, "dist-tags": {}},
            },
            {
                "exitCode": 0,
                "stdout": "",
                "stderr": "",
                "timedOut": False,
                "answer": {
                    "name": "demo",
                    "version": "2.0.0",
                    "engines": {"dsh": "^9.0.0"},
                    "dsh": {"bundle": {}},
                },
            },
        ]
    )

    async def view(*_a: Any, **_k: Any) -> dict[str, Any]:
        return next(answers)

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
    assert listed["versions"][0].get("incompatible") is not None

    monkeypatch.setattr(
        service_module, "attribute_failure", lambda *_a, **_k: "spec-host"
    )
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
    assert (await manager.list_versions("demo"))["status"] == "refused"

    async def listed_only(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"status": "listed", "installed": "1.0.0", "versions": []}

    monkeypatch.setattr(manager, "list_versions", listed_only)
    assert (await manager.update_bundle("demo", "bad"))["step"] == "check"


@pytest.mark.asyncio
async def test_incompatible_after_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        service_module.compatibility,
        "evaluate_plugin_compatibility",
        lambda *_a, **_k: {
            "name": "demo",
            "version": "1.0.0",
            "runtimeVersion": "0.3.0",
            "requirements": {"engines.dsh": "^9"},
            "exempted": False,
        },
    )

    async def local(*_a: Any, **_k: Any) -> tuple[str, str]:
        return "demo", "1.0.0"

    manager = PluginManager(Context(), {"root": str(tmp_path)})
    monkeypatch.setattr(manager, "_install_local_source", local)
    result = await manager.install_bundle("/tmp/demo")
    assert result["application"] == "failed"
    assert result["error"]["code"] == "incompatible-version"


def test_installed_from_spec_single_added(tmp_path: Path) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"demo": "1.0.0"}}), encoding="utf-8"
    )
    name, version = manager._installed_from_spec(
        {},
        {},
        "demo@1.0.0",
        {"kind": "registry", "spec": "demo@1.0.0"},
        {"beforeDependencies": {}},
    )
    assert name == "demo" and version == "1.0.0"


def test_final_new_branches(tmp_path: Path) -> None:
    state = _AbortState({"aborted": True})
    with pytest.raises(InstallCancelledError):
        state.throw_if_aborted()
    snapshot = tmp_path / ".package.json.snapshot"
    snapshot.write_text("{}", encoding="utf-8")
    _restore_snapshot(tmp_path)
    assert (tmp_path / "package.json").read_text(encoding="utf-8") == "{}"

    wrong = tmp_path / "wrong" / "package.json"
    wrong.parent.mkdir(parents=True)
    wrong.write_text(
        json.dumps(
            {"name": "@deepseek-ai/dsh", "dependencies": {"dsh": "workspace:*"}}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="apps/cli"):
        official_install_target.official_bundle_install_target(
            "demo", str(wrong), "0.3.0"
        )
    anchor = tmp_path / "apps" / "cli" / "package.json"
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text(
        json.dumps(
            {"name": "@deepseek-ai/dsh", "dependencies": {"dsh": "workspace:*"}}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="root manifest"):
        official_install_target.official_bundle_install_target(
            "demo", str(anchor), "0.3.0"
        )
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "@deepseek-ai/dsh-root"}), encoding="utf-8"
    )
    package = tmp_path / "packages" / "boot" / "demo"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"name": "demo", "version": "0.3.0", "dsh": {}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="declares no bundle"):
        official_install_target.official_bundle_install_target(
            "demo", str(anchor), "0.3.0"
        )
    manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
    manifest["dsh"] = {"bundle": {"patch": ["p.yml"]}}
    manifest["main"] = "index.js"
    (package / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "index.js").write_text("module.exports = {}", encoding="utf-8")
    with pytest.raises(ValueError, match="patch file"):
        official_install_target.official_bundle_install_target(
            "demo", str(anchor), "0.3.0"
        )


@pytest.mark.asyncio
async def test_catalog_and_install_target(tmp_path: Path) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    assert await manager.catalog() == []
    with pytest.raises(Exception, match="unknown-plugin"):
        await manager.install_target("missing")


def test_last_arcs(tmp_path: Path) -> None:
    assert _AbortState({"aborted": False}).throw_if_aborted() is None

    anchor = tmp_path / "apps" / "cli" / "package.json"
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text(
        json.dumps(
            {"name": "@deepseek-ai/dsh", "dependencies": {"dsh": "workspace:*"}}
        ),
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "@deepseek-ai/dsh-root"}), encoding="utf-8"
    )
    package = tmp_path / "packages" / "boot" / "demo"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"name": "demo", "version": "0.4.0", "dsh": {"bundle": {}}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="runtime version"):
        official_install_target.official_bundle_install_target(
            "demo", str(anchor), "0.3.0"
        )
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "version": "0.3.0",
                "main": "index.js",
                "dsh": {"bundle": {"patch": []}},
            }
        ),
        encoding="utf-8",
    )
    (package / "index.js").write_text("module.exports = {}", encoding="utf-8")
    assert official_install_target.official_bundle_install_target(
        "demo", str(anchor), "0.3.0"
    )["spec"].startswith("link:")


@pytest.mark.asyncio
async def test_watch_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[bool] = []
    process = type("P", (), {"pid": 1, "kill": lambda self: killed.append(True)})()
    signal = type("S", (), {"aborted": True})()
    monkeypatch.setattr(operations.os, "name", "nt")
    await operations._watch_cancel(process, signal)
    assert killed


@pytest.mark.asyncio
async def test_install_target_success(tmp_path: Path) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    with pytest.raises(Exception, match="unknown-plugin"):
        await manager.install_target("missing")


@pytest.mark.asyncio
async def test_remaining_full_branches(tmp_path: Path) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    (tmp_path / "installed" / "demo").mkdir(parents=True)
    image_file = tmp_path / "installed" / "demo" / "a.png"
    image_file.write_bytes(b"png")
    (image_file.parent / "guide.md").write_text("# Guide", encoding="utf-8")
    image = await manager.bundle_image("demo", "guide.md", "a.png")
    assert image["mediaType"] == "image/png"

    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"demo": "1.0.0"}}), encoding="utf-8"
    )
    package = tmp_path / "installed" / "demo"
    (package / "package.json").write_text(
        json.dumps({"name": "demo", "version": "1.0.0", "dsh": {"bundle": {}}}),
        encoding="utf-8",
    )
    published = await manager.install_target("demo")
    assert published == {"spec": "demo@1.0.0", "version": "1.0.0"}

    anchor = tmp_path / "apps" / "cli" / "package.json"
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text(
        json.dumps(
            {"name": "@deepseek-ai/dsh", "dependencies": {"dsh": "workspace:*"}}
        ),
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text(
        json.dumps(
            {"dependencies": {"demo": "1.0.0"}, "name": "@deepseek-ai/dsh-root"}
        ),
        encoding="utf-8",
    )
    bundle = tmp_path / "packages" / "boot" / "demo"
    bundle.mkdir(parents=True)
    (bundle / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "version": "1.0.0",
                "main": "index.js",
                "dsh": {"bundle": {"patch": ["p.yml", {"file": "q.yml"}]}},
            }
        ),
        encoding="utf-8",
    )
    (bundle / "index.js").write_text("module.exports = {}", encoding="utf-8")
    (bundle / "p.yml").write_text("[]", encoding="utf-8")
    manager2 = PluginManager(
        Context(), {"root": str(tmp_path), "installAnchor": str(anchor)}
    )
    assert (await manager2.install_target("demo"))["spec"].startswith("link:")


@pytest.mark.asyncio
async def test_install_reports_overridden(tmp_path: Path) -> None:
    source = tmp_path / "src-demo"
    source.mkdir(parents=True)
    (source / "package.json").write_text(
        json.dumps({"name": "demo", "version": "1.0.0", "dsh": {"bundle": {}}}),
        encoding="utf-8",
    )
    (tmp_path / "plugin-patches.json").write_text(
        json.dumps({"entries": {"row": {"bundle": "demo", "enabled": False}}}),
        encoding="utf-8",
    )
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    result = await manager.install_bundle(str(source))
    assert result["application"] == "overridden"


@pytest.mark.asyncio
async def test_cancel_during_pnpm_is_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})
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

    async def cancelling_run(*_a: Any, signal: Any = None, **_k: Any) -> dict[str, Any]:
        assert signal is not None
        signal._control["aborted"] = True
        return {
            "exitCode": 1,
            "output": "terminated",
            "truncated": False,
            "logPath": "x",
            "timedOut": True,
        }

    monkeypatch.setattr(operations, "run_profile_pnpm", cancelling_run)
    result = await manager.install_bundle("demo@1.0.0", {"requestId": "cancel-pnpm"})
    assert result["application"] == "cancelled"


@pytest.mark.asyncio
async def test_local_install_failure_rolls_back(tmp_path: Path) -> None:
    before = {"dependencies": {}, "dsh": {"profile": {"bundles": []}}}
    (tmp_path / "package.json").write_text(json.dumps(before), encoding="utf-8")
    source = tmp_path / "src-demo"
    source.mkdir()
    (source / "package.json").write_text(
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
    result = await manager.install_bundle(str(source))
    assert result["application"] == "failed"
    assert json.loads((tmp_path / "package.json").read_text(encoding="utf-8")) == before
    assert not (tmp_path / "installed" / "demo").exists()


@pytest.mark.asyncio
async def test_all_changes_run_exclusively(tmp_path: Path) -> None:
    calls: list[str] = []

    class Hmr:
        async def run_exclusive(self, job: Any) -> Any:
            calls.append("exclusive")
            return job()

    ctx = Context()
    ctx.provide("hmr", Hmr())
    manager = PluginManager(ctx, {"root": str(tmp_path)})
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {}, "dsh": {"profile": {"bundles": ["demo"]}}}),
        encoding="utf-8",
    )
    installed = tmp_path / "installed" / "demo"
    installed.mkdir(parents=True)
    (installed / "package.json").write_text(
        json.dumps({"name": "demo", "version": "1.0.0", "dsh": {"bundle": {}}}),
        encoding="utf-8",
    )
    (tmp_path / "plugin-patches.json").write_text(
        json.dumps({"entries": {"r": {"moduleName": "demo", "enabled": True}}}),
        encoding="utf-8",
    )
    assert (await manager.set_bundle_enabled("demo", False))["changed"] is True
    assert (await manager.set_bundle_enabled("demo", True))["changed"] is True
    assert (await manager.set_plugin_enabled("r", False))["changed"] is True
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_invalid_diagnostic_is_dropped(tmp_path: Path) -> None:
    ctx = Context()
    manager = PluginManager(ctx, {"root": str(tmp_path)})
    events: list[Any] = []
    ctx.on("plugin-manager/operation", lambda *_a: events.append(_a))
    manager._emit_operation("settled", {"stage": "bogus", "target": "x"})
    assert events == []


@pytest.mark.asyncio
async def test_failure_during_apply_keeps_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingHmr:
        async def run_exclusive(self, job: Any) -> Any:
            raise ManagementFailure("operation-error", "apply failed")

    ctx = Context()
    ctx.provide("hmr", FailingHmr())
    manager = PluginManager(ctx, {"root": str(tmp_path)})
    source = tmp_path / "src-demo"
    source.mkdir()
    (source / "package.json").write_text(
        json.dumps({"name": "demo", "version": "1.0.0", "dsh": {"bundle": {}}}),
        encoding="utf-8",
    )
    result = await manager.install_bundle(str(source))
    assert result["application"] == "failed"
    assert (tmp_path / "installed" / "demo" / "package.json").exists()


@pytest.mark.asyncio
async def test_rollback_removes_staging(tmp_path: Path) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    staged = tmp_path / "installed" / ".staging"
    staged.mkdir(parents=True)
    (staged / "marker").write_text("x", encoding="utf-8")
    manager._rollback_install({}, set())
    assert not staged.exists()


@pytest.mark.asyncio
async def test_cancel_poll_is_configurable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []
    manager = PluginManager(Context(), {"root": str(tmp_path), "cancelPollMs": 3})
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

    async def run(*_a: Any, cancel_poll_ms: int = 0, **_k: Any) -> dict[str, Any]:
        seen.append(cancel_poll_ms)
        return {
            "exitCode": 1,
            "output": "",
            "truncated": False,
            "logPath": "x",
            "timedOut": False,
        }

    monkeypatch.setattr(operations, "run_profile_pnpm", run)
    assert (await manager.install_bundle("demo@1.0.0"))["application"] == "failed"
    assert seen == [3]
