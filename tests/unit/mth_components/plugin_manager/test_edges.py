"""Edge-case probes for the core plugin manager."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from animas import Context

from mth_components.plugin_manager import (
    InvalidInstallSpecError,
    PluginManager,
    PluginManagerError,
    parse_install_spec,
    pinned_git_origin,
)
from mth_components.plugin_manager.install_spec import dependency_spec
from mth_components.plugin_manager.registry import (
    NPMMIRROR_REGISTRY,
    normalize_registry,
    registry_plan,
)
from mth_components.plugin_manager.service import COMPATIBILITY_FILE, MANIFEST_FILE
from mth_components.plugin_manager.types import (
    InstallCancelledError,
    ManagementFailure,
    management_error,
    read_profile_state,
)


def test_spec_and_registry_edges() -> None:
    with pytest.raises(InvalidInstallSpecError, match="must be absolute"):
        parse_install_spec("file:./relative")
    assert pinned_git_origin("not a name") is None
    with pytest.raises(ValueError, match="http"):
        normalize_registry("http://[::1")
    plan = registry_plan(
        None,
        {
            "registry": NPMMIRROR_REGISTRY,
            "fallbackRegistries": [NPMMIRROR_REGISTRY],
            "resolved": None,
        },
    )
    assert len(plan) == 1
    assert (
        normalize_registry("https://registry.npmjs.org:443/")
        == "https://registry.npmjs.org/"
    )
    assert normalize_registry("https://example.com:8443/").endswith(":8443/")
    assert normalize_registry("https://user:token@registry.example.com/p/") == (
        "https://user:token@registry.example.com/p/"
    )
    assert normalize_registry("https://[2001:db8::1]:443/foo") == (
        "https://[2001:db8::1]/foo/"
    )
    assert (
        normalize_registry("https://registry.npmjs.org:443/")
        == "https://registry.npmjs.org/"
    )
    assert normalize_registry("https://example.com/p?token=abc#frag").endswith(
        "?token=abc#frag"
    )
    assert management_error(ManagementFailure("not-bundle", "x", [{"name": "p"}]))[
        "incompatible"
    ] == [{"name": "p"}]
    assert management_error(RuntimeError("boom")) == {
        "code": "operation-error",
        "diagnostic": "boom",
    }
    assert management_error(RuntimeError())["diagnostic"] == "RuntimeError"
    assert dependency_spec("p", "1", "/x").startswith("p@")


def test_constructor_edges(tmp_path: Path) -> None:
    ctx = Context()
    with pytest.raises(ValueError, match="root"):
        PluginManager(ctx, {})
    manager = PluginManager(ctx, {"root": str(tmp_path)})
    with pytest.raises(RuntimeError, match="already registered"):
        PluginManager(ctx, {"root": str(tmp_path)})
    (tmp_path / "package.json").write_text("[]", encoding="utf-8")
    with pytest.raises(PluginManagerError):
        manager._manifest()
    manager._dispose()
    assert manager._disposed


@pytest.mark.asyncio
async def test_cancellation_and_list_edges(tmp_path: Path) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    original = manager._patches

    def cancelled() -> Any:
        raise InstallCancelledError()

    manager._patches = cancelled  # type: ignore[method-assign]
    result = manager.set_plugin_enabled("any", True)
    assert result["application"] == "cancelled"
    manager._patches = original  # type: ignore[method-assign]

    (tmp_path / "installed").mkdir()
    (tmp_path / "installed" / "plain").mkdir()
    (tmp_path / "installed" / "plain" / "package.json").write_text(
        json.dumps({"name": "plain", "version": "1"}), encoding="utf-8"
    )
    (tmp_path / "package.json").write_text(
        json.dumps(
            {"dependencies": {"plain": "1.0"}, "dsh": {"profile": {"bundles": []}}}
        ),
        encoding="utf-8",
    )
    bundles = await manager.list_bundles()
    plain = next(item for item in bundles if item["name"] == "plain")
    assert plain["error"] == {"code": "not-bundle"}


@pytest.mark.asyncio
async def test_exemption_and_bundle_edges(tmp_path: Path) -> None:
    manager = PluginManager(
        Context(),
        {
            "root": str(tmp_path),
            "protectedPlugins": ["protected"],
            "officialBundles": ["official"],
        },
    )
    manager_root = tmp_path
    source = manager_root / "src"
    source.mkdir()
    (source / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "version": "1.0.0",
                "dsh": {"bundle": {"rows": [{"id": "r", "module": "m"}, "skip"]}},
            }
        ),
        encoding="utf-8",
    )
    assert manager.install_bundle(str(source), {"enabled": False})["bundle"] == "demo"
    assert (
        manager.set_version_exemption("not a name", "1", True, True)["error"]["code"]
        == "invalid-spec"
    )
    assert (
        manager.set_version_exemption(str(source), "1", True, True)["error"]["code"]
        == "invalid-spec"
    )
    assert manager.set_version_exemption("demo@1.0.0", "0.3.0", True, True)["changed"]
    assert manager.set_version_exemption("demo@1.0.0", "0.3.0", False)["changed"]
    assert manager.list_version_exemptions()["exemptions"] == {}

    protected_source = manager_root / "protected-src"
    protected_source.mkdir()
    (protected_source / "package.json").write_text(
        json.dumps(
            {"name": "protected", "version": "1", "dsh": {"bundle": {"rows": []}}}
        ),
        encoding="utf-8",
    )
    assert manager.install_bundle(str(protected_source))["bundle"] == "protected"
    assert (
        manager.set_bundle_enabled("protected", False)["error"]["code"]
        == "management-required"
    )
    official_source = manager_root / "official-src"
    official_source.mkdir()
    (official_source / "package.json").write_text(
        json.dumps(
            {"name": "official", "version": "1", "dsh": {"bundle": {"rows": []}}}
        ),
        encoding="utf-8",
    )
    manager.install_bundle(str(official_source))
    assert (
        manager.set_bundle_enabled("official", False)["error"]["code"]
        == "not-removable"
    )
    assert manager.remove_bundle("official")["error"]["code"] == "not-removable"
    assert manager.remove_bundle("protected")["error"]["code"] == "not-removable"
    assert manager.set_bundle_enabled("demo", True)["application"] == "restart-required"
    assert manager.remove_bundle("demo")["application"] == "restart-required"

    bundles = await manager.list_bundles()
    assert any(item.get("readOnlyReason") == "management-required" for item in bundles)


def test_install_rollback_branches(tmp_path: Path) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    (tmp_path / "installed").mkdir()
    (tmp_path / "installed" / ".staging").mkdir()

    nameless = tmp_path / "nameless"
    nameless.mkdir()
    (nameless / "package.json").write_text(
        json.dumps({"dsh": {"bundle": {"rows": []}}}), encoding="utf-8"
    )
    assert manager.install_bundle(str(nameless))["error"]["code"] == "not-bundle"

    duplicate = tmp_path / "duplicate"
    duplicate.mkdir()
    (duplicate / "package.json").write_text(
        json.dumps(
            {"name": "duplicate", "version": "1", "dsh": {"bundle": {"rows": []}}}
        ),
        encoding="utf-8",
    )
    manager.install_bundle(str(duplicate))
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {}, "dsh": {"profile": {"bundles": []}}}),
        encoding="utf-8",
    )
    assert manager.install_bundle(str(duplicate))["application"] == "restart-required"


def _git_repo(tmp_path: Path) -> tuple[str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        json.dumps(
            {"name": "gitbundle", "version": "1.0.0", "dsh": {"bundle": {"rows": []}}}
        ),
        encoding="utf-8",
    )
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@e.st"],
        ["git", "config", "user.name", "test"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "init"],
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return str(repo), commit


def test_git_install_and_failures(tmp_path: Path) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    repo, commit = _git_repo(tmp_path)
    result = manager.install_bundle(f"git+file://{repo}#{commit}")
    assert (
        result["bundle"] == "gitbundle" and result["application"] == "restart-required"
    )

    (tmp_path / "installed" / "gitbundle").rename(tmp_path / "installed" / "moved")
    manager2 = PluginManager(Context(), {"root": str(tmp_path / "other")})
    (tmp_path / "other").mkdir(exist_ok=True)
    assert (
        manager2.install_bundle(f"https://invalid.example/x/y#{commit}")["error"][
            "code"
        ]
        == "operation-error"
    )
    assert (
        manager2.install_bundle(f"{repo}#{'f' * 40}")["error"]["code"] == "invalid-spec"
    )


def test_remaining_service_branches(tmp_path: Path) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {},
                "dsh": {"profile": {"bundles": ["selected"]}},
            }
        ),
        encoding="utf-8",
    )
    source = tmp_path / "selected-src"
    source.mkdir()
    (source / "package.json").write_text(
        json.dumps({"name": "selected", "dsh": {"bundle": {"rows": []}}}),
        encoding="utf-8",
    )
    result = manager.install_bundle(str(source))
    assert result["bundle"] == "selected" and "version" not in result

    (tmp_path / "installed" / "selected" / "package.json").write_text(
        json.dumps(
            {"name": "selected", "version": "1", "dsh": {"bundle": {"rows": []}}}
        ),
        encoding="utf-8",
    )
    (tmp_path / "plugin-patches.json").write_text(
        json.dumps({"entries": {"row": {"bundle": "selected", "enabled": True}}}),
        encoding="utf-8",
    )
    assert manager.remove_bundle("selected")["error"]["code"] == "bundle-in-use"

    (tmp_path / "plugin-patches.json").write_text(
        json.dumps({"entries": {}}), encoding="utf-8"
    )
    manifest = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    manifest["dependencies"] = {"selected": "file:./selected-src"}
    (tmp_path / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    shutil.rmtree(tmp_path / "installed" / "selected")
    assert manager.remove_bundle("selected")["application"] == "restart-required"


def test_git_fragment_without_commit(tmp_path: Path) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    repo, _commit = _git_repo(tmp_path)
    assert (
        manager.install_bundle(f"git+file://{repo}#path:only")["error"]["code"]
        == "invalid-spec"
    )


def test_git_checkout_failure(tmp_path: Path) -> None:
    root = tmp_path / "other"
    root.mkdir()
    manager = PluginManager(Context(), {"root": str(root)})
    (root / "installed").mkdir()
    repo, _commit = _git_repo(tmp_path)
    assert (
        manager.install_bundle(f"git+file://{repo}#{'f' * 40}")["error"]["code"]
        == "invalid-spec"
    )


def test_compatibility_file_edges(tmp_path: Path) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    compatibility = tmp_path / "compatibility.json"

    compatibility.write_text("{bad", encoding="utf-8")
    first = manager.list_version_exemptions()
    assert first["exemptions"] == {} and first["warnings"]

    compatibility.write_text("[]", encoding="utf-8")
    assert manager.list_version_exemptions()["warnings"]

    compatibility.write_text(
        json.dumps(
            {
                "bad-key": ["0.3.0"],
                "demo@1.0.0": "not-a-list",
                "valid@1.0.0": ["0.3.0"],
            }
        ),
        encoding="utf-8",
    )
    listed = manager.list_version_exemptions()
    assert listed["exemptions"] == {"valid@1.0.0": ["0.3.0"]}
    assert len(listed["warnings"]) >= 2
    assert (
        manager.set_version_exemption("valid@1.0.0", "0.3.0", False)["error"]["code"]
        == "operation-error"
    )

    compatibility.unlink()
    assert manager.list_version_exemptions()["exemptions"] == {}
    assert (
        manager.set_version_exemption("demo@^1.0.0", "0.3.0", True, True)["error"][
            "code"
        ]
        == "invalid-spec"
    )
    assert (
        manager.set_version_exemption("demo@1.0.0", "9.9.9", True, True)["error"][
            "code"
        ]
        == "operation-error"
    )
    manager._dispose()
    with pytest.raises(PluginManagerError, match="disposed"):
        manager._guard()


def test_noop_and_selected_removal(tmp_path: Path) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {},
                "dsh": {"profile": {"bundles": ["ghost"]}},
            }
        ),
        encoding="utf-8",
    )
    assert manager.set_bundle_enabled("ghost", True)["changed"] is False
    assert manager.set_bundle_enabled("ghost", False)["changed"] is True
    assert manager.set_bundle_enabled("ghost", False)["changed"] is False
    assert (
        manager.set_bundle_enabled("ghost", True)["error"]["code"] == "unknown-plugin"
    )
    assert manager.remove_bundle("absent")["error"]["code"] == "not-removable"
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {}, "dsh": {"profile": {"bundles": ["ghost2"]}}}),
        encoding="utf-8",
    )
    assert manager.remove_bundle("ghost2")["changed"] is True


def test_read_profile_state_reraise() -> None:
    original = PluginManagerError("already", "PLUGIN_MANAGER_UNAVAILABLE")
    with pytest.raises(PluginManagerError) as caught:
        read_profile_state(lambda: (_ for _ in ()).throw(original))
    assert caught.value is original


def test_disk_state_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})

    def failing_read(self: Path, *_args: Any, **_kwargs: Any) -> str:
        raise OSError("unreadable")

    monkeypatch.setattr(Path, "read_text", failing_read)
    assert manager._disk_state() == "\0" * 4


def test_profile_read_and_disk_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PluginManager(Context(), {"root": str(tmp_path)})
    (tmp_path / MANIFEST_FILE).write_text("{}", encoding="utf-8")
    compatibility = tmp_path / COMPATIBILITY_FILE
    compatibility.write_text("{}", encoding="utf-8")

    def failing_read(self: Path, *_args: Any, **_kwargs: Any) -> str:
        raise OSError("denied")

    monkeypatch.setattr(Path, "read_text", failing_read)
    with pytest.raises(PluginManagerError, match="failed to read"):
        manager._manifest()
    assert manager._disk_state() == "\0" * 4
    assert manager._compatibility()[2] is False
