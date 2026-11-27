"""Unit tests for core profile plugin management."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from animas import Context

from mth_components.plugin_manager import (
    InvalidInstallSpecError,
    PluginManager,
    dependency_spec,
    parse_install_spec,
    pinned_git_origin,
)
from mth_components.plugin_manager.component import apply
from mth_components.plugin_manager.registry import (
    NPMMIRROR_REGISTRY,
    attribute_failure,
    normalize_registry,
    registry_plan,
)
from mth_components.plugin_manager.types import (
    PluginManagerError,
    read_profile_state,
)

COMMIT = "a" * 40


def _bundle(
    root: Path, name: str, *, version: str = "1.0.0", rows: bool = True
) -> Path:
    source = root / f"src-{name}"
    source.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "name": name,
        "version": version,
        "description": f"bundle {name}",
    }
    if rows:
        manifest["dsh"] = {
            "bundle": {
                "rows": [
                    {"id": f"{name}-row", "module": f"{name}.module"},
                    {"group": "ignored", "config": []},
                ]
            }
        }
    (source / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    return source


def _ctx(root: Path, **config: Any) -> Context:
    ctx = Context()
    ctx.provide(
        "pluginManager",
        PluginManager(ctx, {"root": str(root), **config}),
    )
    return ctx


def _write_manifest(root: Path, **overrides: Any) -> None:
    manifest: dict[str, Any] = {
        "dependencies": {},
        "dsh": {"profile": {"bundles": []}},
    }
    manifest.update(overrides)
    (root / "package.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_install_spec_parsing(tmp_path: Path) -> None:
    assert parse_install_spec("  pkg  ") == {
        "kind": "registry",
        "spec": "pkg",
        "name": "pkg",
    }
    assert parse_install_spec("@scope/pkg@^1")["range"] == "^1"
    for bad, reason in (
        ("", "must not be empty"),
        ("./relative", "must be absolute"),
        ("https://x/y", "git repository or a tarball"),
        ("not a name", "not a package name"),
        ("pkg@", "must not be empty"),
    ):
        with pytest.raises(InvalidInstallSpecError, match=reason):
            parse_install_spec(bad)
    assert parse_install_spec(str(tmp_path))["kind"] == "path"
    assert parse_install_spec(f"link:{tmp_path}")["kind"] == "path"
    assert parse_install_spec(f"file:{tmp_path / 'a.tgz'}")["kind"] == "tarball"
    assert parse_install_spec("github:user/repo")["host"] == "github.com"
    assert parse_install_spec("git@host.tld:user/repo")["host"] == "host.tld"
    assert parse_install_spec("https://example.com/a/b")["kind"] == "git"
    assert parse_install_spec("https://example.com/a.tgz")["host"] == "example.com"

    origin = pinned_git_origin(f"https://github.com/u/r.git#{COMMIT}&path:sub/dir")
    assert origin == "github.com/u/r#sub/dir"
    assert pinned_git_origin("pkg") is None
    assert pinned_git_origin("github:u/r") is None
    assert pinned_git_origin("github:u/r#bad&path:x&path:y") is None
    assert pinned_git_origin(f"github:u/r#{'b' * 40}&other=x") is None
    assert pinned_git_origin(f"github:u/r#{COMMIT}") == "github.com/u/r#"

    assert dependency_spec("pkg", "1.0", str(tmp_path)) == "pkg@1.0"
    assert dependency_spec(
        "pkg", f"git+https://user@host/x#{COMMIT}", str(tmp_path)
    ).startswith("git+https://host/")
    assert (
        dependency_spec("pkg", "file:./local", str(tmp_path))
        == f"file:{tmp_path / 'local'}"
    )
    assert dependency_spec("pkg", "file:~/home", str(tmp_path)).startswith("file:")
    assert dependency_spec(
        "alias", f"git+https://host/x#{COMMIT}", str(tmp_path), "real"
    ).startswith("alias@git+")


def test_registry_helpers() -> None:
    assert normalize_registry("https://Example.com/foo") == "https://example.com/foo/"
    with pytest.raises(ValueError, match="http"):
        normalize_registry("not-a-url")
    configured = {
        "registry": None,
        "fallbackRegistries": [NPMMIRROR_REGISTRY],
        "resolved": None,
    }
    assert registry_plan(None, configured)[0] is None
    assert registry_plan("https://private.example/", configured) == [
        "https://private.example/"
    ]
    configured["resolved"] = "https://registry.npmjs.org/"
    plan = registry_plan(None, configured)
    assert plan[0] is None and len(plan) >= 1
    plan2 = registry_plan(NPMMIRROR_REGISTRY, configured)
    assert plan2[0] == NPMMIRROR_REGISTRY
    assert attribute_failure("network", "boom", {"kind": "registry"}) == "registry"
    assert attribute_failure("disk-full", "boom", {}) == "other"
    assert (
        attribute_failure(
            "network", "fatal: example.com unreachable", {"host": "example.com"}
        )
        == "spec-host"
    )


def test_error_helpers() -> None:
    with pytest.raises(PluginManagerError, match="failed to read the profile"):
        read_profile_state(lambda: (_ for _ in ()).throw(OSError("bad")))
    with pytest.raises(PluginManagerError, match="cannot convert"):
        read_profile_state(
            lambda: (_ for _ in ()).throw(ZeroDivisionError("cannot convert"))
        )


@pytest.mark.asyncio
async def test_install_list_enable_remove(tmp_path: Path) -> None:
    source = _bundle(tmp_path, "demo")
    ctx = _ctx(tmp_path)
    manager: PluginManager = ctx.pluginManager
    result = manager.install_bundle(str(source))
    assert result["application"] == "restart-required"
    assert result["bundle"] == "demo" and result["version"] == "1.0.0"
    assert (tmp_path / "installed" / "demo" / "package.json").exists()

    bundles = await manager.list_bundles()
    demo = next(item for item in bundles if item["name"] == "demo")
    assert demo["installed"] and demo["enabled"] and demo["rows"]
    assert manager.install_bundle(str(source))["error"]["code"] == "already-installed"

    assert (
        manager.set_bundle_enabled("demo", False)["application"] == "restart-required"
    )
    assert (
        manager.set_bundle_enabled("missing", True)["error"]["code"] == "unknown-plugin"
    )
    plugins = await manager.list_plugins()
    row = next(item for item in plugins if item["moduleName"] == "demo.module")
    assert manager.set_plugin_enabled(row["entryId"], False)["changed"] is True
    assert (
        manager.set_plugin_enabled("missing", True)["error"]["code"] == "unknown-plugin"
    )
    assert (
        manager.set_version_exemption("demo@1.0.0", "0.3.0", True, True)["application"]
        == "restart-required"
    )
    assert manager.list_version_exemptions()["exemptions"] == {"demo@1.0.0": ["0.3.0"]}
    assert (
        manager.set_version_exemption("demo@1.0.0", "0.3.0", True)["error"]["code"]
        == "operation-error"
    )
    assert manager.remove_bundle("demo")["application"] == "restart-required"
    assert not (tmp_path / "installed" / "demo").exists()


@pytest.mark.asyncio
async def test_install_failures_and_inspect(tmp_path: Path) -> None:
    manager: PluginManager = _ctx(tmp_path).pluginManager
    assert manager.install_bundle("")["error"]["code"] == "invalid-spec"
    assert manager.install_bundle("pkg")["error"]["code"] == "operation-error"
    assert manager.install_bundle("/absent/path")["error"]["code"] == "invalid-spec"

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "package.json").write_text(
        json.dumps({"name": "plain", "version": "1"}), encoding="utf-8"
    )
    assert manager.install_bundle(str(plain))["error"]["code"] == "not-bundle"

    no_manifest = tmp_path / "empty"
    no_manifest.mkdir()
    assert manager.install_bundle(str(no_manifest))["error"]["code"] == "not-bundle"

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "package.json").write_text("{invalid", encoding="utf-8")
    assert manager.install_bundle(str(broken))["error"]["code"] == "not-bundle"

    assert manager.inspect("pkg")["status"] == "accepted"
    assert manager.inspect("not a name")["status"] == "refused"
    assert manager.inspect(str(plain))["bundle"] is False
    assert manager.inspect(str(no_manifest))["bundle"] is False
    assert (await manager.registries())["resolved"] is None


@pytest.mark.asyncio
async def test_protected_and_profile_read_failures(tmp_path: Path) -> None:
    manager: PluginManager = _ctx(
        tmp_path,
        protectedPlugins=["managed"],
        officialBundles=["official"],
        ownerEntryId="owner",
    ).pluginManager
    (tmp_path / "plugin-patches.json").write_text(
        json.dumps(
            {
                "entries": {
                    "owner": {"moduleName": "managed", "enabled": True},
                    "other": {"moduleName": "other", "enabled": True},
                }
            }
        ),
        encoding="utf-8",
    )
    _write_manifest(tmp_path)
    plugins = await manager.list_plugins()
    assert (
        next(p for p in plugins if p["entryId"] == "owner")["readOnlyReason"]
        == "management-required"
    )
    assert "patchId" in next(p for p in plugins if p["entryId"] == "other")
    assert (
        manager.set_plugin_enabled("owner", False)["error"]["code"]
        == "management-required"
    )

    bundles = await manager.list_bundles()
    official = next(b for b in bundles if b["name"] == "official")
    assert official["official"] and official["availability"] == "missing"
    assert manager.set_bundle_enabled("official", False)["changed"] is False

    (tmp_path / "package.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(PluginManagerError, match="failed to read the profile"):
        await manager.list_bundles()
    manager._dispose()
    with pytest.raises(PluginManagerError, match="disposed"):
        await manager.list_plugins()


@pytest.mark.asyncio
async def test_component_apply(tmp_path: Path) -> None:
    ctx = Context()
    await apply(ctx, root=str(tmp_path))
    assert isinstance(ctx.pluginManager, PluginManager)
