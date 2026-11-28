"""Branch probes for plugin-manager helper modules."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from mth_components.plugin_manager import (
    build_approval,
    bundle_detail,
    catalog,
    compatibility,
    git_transport,
    github_connection,
    official_install_target,
    semver,
    versions,
)
from mth_components.plugin_manager.types import ManagementFailure, RemoteError


def test_semver_edges() -> None:
    assert semver.compare("1.0.0", "1.0.0-alpha") == 1
    assert semver.compare("1.0.0-alpha", "1.0.0") == -1
    assert semver.compare("1.0.0-alpha.1", "1.0.0-alpha.2") == -1
    assert semver.compare("1.0.0-alpha.1", "1.0.0-alpha.beta") == -1
    assert semver.compare("1.0.0-beta", "1.0.0-beta.1") == -1
    assert semver.compare("1.0.0-alpha.1", "1.0.0-alpha.1.1") == -1
    assert semver.satisfies("0.0.3", "^0.0.3")
    assert not semver.satisfies("0.0.4", "^0.0.3")
    assert not semver.satisfies("1.0.0", "^bad")
    assert not semver.satisfies("1.0.0", "~bad")
    assert semver.satisfies("1.2.3", "1.2.x")
    assert not semver.satisfies("1.3.0", "1.2.x")
    assert not semver.satisfies("1.0.0", "wat")
    assert semver.satisfies("1.0.0", "|| >=1.0.0")


def test_versions_edges() -> None:
    item = versions.version_facts(
        {"engines": {"node": ">=20"}, "peerDependencies": {"x": "1"}, "deprecated": ""},
        "p",
        "1.0.0",
    )
    assert item["engines"] == {"node": ">=20"} and "deprecated" not in item
    assert versions.newer_versions({"versions": ["1.1.0-beta.1"]}, "1.0.0")[0][
        "prerelease"
    ]


def test_catalog_edges() -> None:
    assert (
        catalog.bundle_catalog_entry({"name": "x", "meta": {"title": ["bad"]}})["title"]
        == "x"
    )
    assert (
        catalog.bundle_catalog_entry({"name": "x", "description": "d"})["description"]
        == "d"
    )


def test_compatibility_edges() -> None:
    with pytest.raises(ValueError, match="engines"):
        compatibility.unmet_requirements({"engines": []}, "0.3.0")
    with pytest.raises(ValueError, match="string"):
        compatibility.unmet_requirements({"engines": {"dsh": 1}}, "0.3.0")
    with pytest.raises(ValueError, match="peerDependencies"):
        compatibility.unmet_requirements({"peerDependencies": []}, "0.3.0")
    with pytest.raises(ValueError, match="string"):
        compatibility.unmet_requirements(
            {"peerDependencies": {"@deepseek-ai/dsh": 1}}, "0.3.0"
        )
    assert (
        compatibility.unmet_requirements({"peerDependencies": {"other": "^1"}}, "0.3.0")
        == {}
    )
    with pytest.raises(ValueError, match="version"):
        compatibility.evaluate_plugin_compatibility(
            {"name": "x", "engines": {"dsh": "^9"}}, {}, "0.3.0"
        )


def test_build_approval_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "pnpm-workspace.yaml"
    workspace.write_text("[]", encoding="utf-8")
    with pytest.raises(ManagementFailure, match="mapping"):
        build_approval.read_policy(str(tmp_path))
    workspace.chmod(0o000)
    with pytest.raises(ManagementFailure, match="cannot read"):
        build_approval.read_policy(str(tmp_path))
    workspace.chmod(0o600)

    assert build_approval._carries_credential("git+ssh://git@host/x") is False
    assert build_approval._carries_credential("p@git+ssh://git@host/x") is False

    workspace.write_text("allowBuilds:\n  a: true", encoding="utf-8")
    build_approval._set_entries(str(tmp_path), {"b": "true"})
    text = workspace.read_text(encoding="utf-8")
    assert text.endswith("\n") and "a: true" in text and "b: true" in text

    workspace.write_text(
        "other: 1\nallowBuilds:\n  a: true # note\nnext: 2\n", encoding="utf-8"
    )
    build_approval._set_entries(str(tmp_path), {"a": "false", "z": "true"})
    text = workspace.read_text(encoding="utf-8")
    assert "a: false # note" in text and "z: true" in text and "next: 2" in text

    workspace.write_text(
        "allowBuilds:\n  a: set this to true or false\n", encoding="utf-8"
    )
    before = workspace.read_text(encoding="utf-8")
    assert build_approval.record_pending_builds(str(tmp_path), "") == ["a"]
    assert workspace.read_text(encoding="utf-8") == before


def test_git_transport_edges() -> None:
    spec = {
        "kind": "git",
        "spec": "git+https://git.example.com/u/r#abc",
        "host": "git.example.com",
    }
    assert git_transport.ssh_git_spec({"kind": "registry"}) is None
    assert (
        git_transport.ssh_git_spec(
            {"kind": "git", "spec": "https://user@git.example.com/u"}
        )
        is None
    )
    assert (
        git_transport.ssh_git_spec(
            {"kind": "git", "spec": "https://git.example.com/u?x=1"}
        )
        is None
    )
    assert (
        git_transport.ssh_git_spec({"kind": "git", "spec": "https://github.com/u/r"})
        is None
    )
    assert git_transport.ssh_git_spec({"kind": "git", "spec": "git@host:u/r"}) is None

    async def fail(*_a: Any, **_k: Any) -> Any:
        raise OSError("no git")

    assert asyncio.run(git_transport.readable("x", "/tmp", spawn=fail)) is False
    assert (
        asyncio.run(
            git_transport.choose_git_transport(
                {"kind": "git", "spec": "github:u/r"}, "/tmp"
            )
        )
        == "github:u/r"
    )
    assert (
        asyncio.run(git_transport.choose_git_transport(spec, "/tmp", spawn=fail))
        == spec["spec"]
    )


def test_github_edges(tmp_path: Path) -> None:
    assert github_connection.is_github_spec({"kind": "git", "host": "x.github.com:443"})
    assert (
        github_connection.repository_url(
            {"kind": "git", "spec": "git+https://github.com/u/r#x"}
        )
        == "https://github.com/u/r"
    )
    assert (
        github_connection.repository_url(
            {"kind": "git", "spec": "git@github.com:u/r#x"}
        )
        == "git@github.com:u/r"
    )


def test_bundle_detail_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert bundle_detail.read_bundle_document(str(tmp_path), "absent", "en") == {
        "status": "none"
    }
    with pytest.raises(RemoteError, match="Invalid image path"):
        bundle_detail.read_bundle_image(str(tmp_path), "demo", "x.md", "../a.png")
    with pytest.raises(RemoteError, match="unavailable"):
        bundle_detail.read_bundle_image(str(tmp_path), "demo", "x.md", "a.png")
    assert bundle_detail.language_chain("zh-CN") == ["zh-cn", "zh", "en"]
    assert bundle_detail.detail_image("x.md", "a.webp") is not None
    assert bundle_detail.detail_image("x.md", "a.txt") is None

    class Response:
        status_code = 200
        content = b"remote"

        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

    monkeypatch.setattr(bundle_detail.httpx, "get", Response)
    found = bundle_detail.read_bundle_document(
        str(tmp_path), "absent", "en", sources=["https://cdn/"]
    )
    assert found["origin"] == "https://cdn/" and found["markdown"] == "remote"
    image = bundle_detail.read_bundle_image(
        str(tmp_path), "demo", "x.md", "a.png", sources=["https://cdn/"]
    )
    assert image["bytes"] == b"remote"


def test_official_target_edges(tmp_path: Path) -> None:
    anchor = tmp_path / "apps" / "cli" / "package.json"
    anchor.parent.mkdir(parents=True)
    anchor.write_text(
        json.dumps(
            {"name": "@deepseek-ai/dsh", "dependencies": {"dsh": "workspace:*"}}
        ),
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "@deepseek-ai/dsh-root"}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unique package"):
        official_install_target.official_bundle_install_target(
            "demo", str(anchor), "0.3.0"
        )
    package = tmp_path / "packages" / "boot" / "demo"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"name": "demo", "version": "0.3.0", "dsh": {"bundle": {}}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not built"):
        official_install_target.official_bundle_install_target(
            "demo", str(anchor), "0.3.0"
        )
    manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
    manifest["main"] = "index.js"
    (package / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "index.js").write_text("module.exports = {}", encoding="utf-8")
    target = official_install_target.official_bundle_install_target(
        "demo", str(anchor), "0.3.0"
    )
    assert target["spec"].startswith("link:")
