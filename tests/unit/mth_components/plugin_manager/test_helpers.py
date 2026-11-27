"""Unit tests for the fully ported plugin-manager helper modules."""

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
    diagnostic,
    git_transport,
    github_connection,
    install_failure,
    install_spec,
    official_install_target,
    operations,
    semver,
    versions,
)
from mth_components.plugin_manager.types import (
    ManagementFailure,
    RemoteError,
)


class FakeProcess:
    def __init__(self, code: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = code
        self.pid = 1234
        self._stdout_bytes = stdout
        self._stderr_bytes = stderr
        self._out = asyncio.StreamReader()
        self._err = asyncio.StreamReader()
        self._out.feed_data(stdout)
        self._out.feed_eof()
        self._err.feed_data(stderr)
        self._err.feed_eof()
        self.stdout = self._out
        self.stderr = self._err
        self.waited = False

    async def wait(self) -> int:
        self.waited = True
        return self.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout_bytes, self._stderr_bytes

    def kill(self) -> None:
        self.returncode = 1


def test_semver() -> None:
    assert semver.is_valid("1.2.3") and not semver.is_valid("1.2")
    assert semver.gt("2.0.0", "1.9.9")
    assert not semver.gt("1.0.0", "1.0.0")
    assert semver.rcompare("2.0.0", "1.0.0") < 0
    assert semver.prerelease("1.0.0-beta.1") == ("beta", 1)
    assert semver.prerelease("1.0.0") is None
    assert semver.satisfies("1.2.3", "^1.0.0")
    assert not semver.satisfies("2.0.0", "^1.0.0")
    assert semver.satisfies("0.2.5", "^0.2.0")
    assert not semver.satisfies("1.2.2", "^1.2.3")
    assert not semver.satisfies("1.2.2", "~1.2.3")
    assert semver.satisfies("1.2.3", "~1.2.0")
    assert not semver.satisfies("1.3.0", "~1.2.0")
    assert semver.satisfies("1.2.3", ">=1.0.0 <2.0.0")
    assert semver.satisfies("1.2.x", "1.2.x") is False
    assert semver.satisfies("1.2.3", "*")
    assert not semver.satisfies("bad", "*")
    with pytest.raises(ValueError):
        semver.compare("bad", "1.0.0")


def test_failure_classification() -> None:
    cases = (
        ("ERR_PNPM_IGNORED_BUILDS", "build-blocked"),
        ("ERR_PNPM_FETCH_404", "not-found"),
        ("ERR_PNPM_NO_MATCHING_VERSION", "no-matching-version"),
        ("ENOSPC", "disk-full"),
        ("EACCES", "permission"),
        ("EINTEGRITY", "integrity"),
        ("The requested URL returned error: 403", "unknown"),
        ("ENOTFOUND", "network"),
        ("", "unknown"),
    )
    for log, expected in cases:
        assert install_failure.classify_install_failure(log) == expected
    assert install_failure.classify_install_failure("", timed_out=True) == "timeout"
    assert (
        install_failure.classify_install_failure("", OSError(2, "missing"))
        == "pnpm-missing"
    )


def test_versions_and_catalog() -> None:
    answer = {
        "versions": ["1.0.0", "2.0.0", "1.5.0", "bad"],
        "time": {"2.0.0": "2026-01-01"},
        "dist-tags": {"latest": "2.0.0"},
    }
    listed = versions.newer_versions(answer, "1.0.0")
    assert [item["version"] for item in listed] == ["2.0.0", "1.5.0"]
    assert listed[0]["publishedAt"] == "2026-01-01"
    assert listed[0]["tags"] == ["latest"]
    assert not listed[0]["prerelease"]
    assert (
        versions.newer_versions({"versions": "2.0.0"}, "1.0.0")[0]["version"] == "2.0.0"
    )
    with pytest.raises(ValueError, match="no versions"):
        versions.newer_versions({}, "1.0.0")
    facts = versions.version_facts(
        {
            "name": "demo",
            "version": "2.0.0",
            "dsh": {"bundle": {}},
            "deprecated": "old",
        },
        "fallback",
        "1.0.0",
    )
    assert facts["bundle"] and facts["deprecated"] == "old"
    assert versions.version_facts({}, "fallback", "1.0.0")["name"] == "fallback"
    entry = catalog.bundle_catalog_entry(
        {
            "name": "demo",
            "description": "plain",
            "meta": {"title": {"en": "Title"}, "description": {"en": "Detail"}},
            "enabled": True,
        }
    )
    assert entry["title"] == "Title" and entry["description"] == "Detail"
    assert catalog.bundle_catalog_entry({"name": "x"})["description"] == ""


def test_diagnostic_validation() -> None:
    valid = {
        "runId": "12345678-1234-4234-8234-123456789abc",
        "operation": {
            "id": "12345678-1234-4234-8234-123456789012",
            "time": "2026-01-01T00:00:00+00:00",
            "reason": "install",
            "stage": "install",
            "target": "demo",
            "phase": "settled",
            "application": "applied",
            "changed": True,
        },
    }
    assert diagnostic.parse_operation_record(valid) is not None
    assert diagnostic.parse_operation_record({"bad": 1}) is None


def test_build_approval(tmp_path: Path) -> None:
    assert build_approval.read_policy(str(tmp_path)) == {}
    workspace = tmp_path / "pnpm-workspace.yaml"
    workspace.write_text(
        "packages:\n  - 'packages/*'\nallowBuilds:\n  allowed: true\n  # keep me\n",
        encoding="utf-8",
    )
    policy = build_approval.read_policy(str(tmp_path))
    assert policy["allowed"] is True
    names = build_approval.blocked_build_names(
        "Ignored build scripts: foo, bar\n"
        "ERR_PNPM_GIT_DEP_PREPARE_NOT_ALLOWED\nallowBuilds:\n  gitdep: true\n"
    )
    assert set(names) == {"foo", "bar", "gitdep"}
    assert (
        build_approval.blocked_build_names(
            "Ignored build scripts: user@git+https://user:pass@host/x\n"
        )
        == []
    )
    pending = build_approval.record_pending_builds(
        str(tmp_path), "Ignored build scripts: foo\n"
    )
    assert pending == ["foo"]
    assert build_approval.pending_builds(build_approval.read_policy(str(tmp_path))) == [
        "foo"
    ]
    build_approval.approve_builds(str(tmp_path), ["foo"])
    assert build_approval.read_policy(str(tmp_path))["foo"] is True
    with pytest.raises(ManagementFailure, match="no longer pending"):
        build_approval.approve_builds(str(tmp_path), ["foo"])
    build_approval.approve_builds(str(tmp_path), [])
    workspace.write_text("allowBuilds: [a, b]\n", encoding="utf-8")
    with pytest.raises(ManagementFailure, match="plain mapping"):
        build_approval.read_policy(str(tmp_path))


def test_compatibility_and_install_spec() -> None:
    assert compatibility.unmet_requirements({}, "0.3.0") == {}
    assert compatibility.unmet_requirements(
        {"engines": {"dsh": "^9.0.0"}}, "0.3.0"
    ) == {"engines.dsh": "^9.0.0"}
    assert (
        compatibility.unmet_requirements(
            {"peerDependencies": {"@deepseek-ai/dsh": "workspace:^"}}, "0.3.0"
        )
        == {}
    )
    assert compatibility.unmet_requirements(
        {"peerDependencies": {"@deepseek-ai/dsh": "^9"}}, "0.3.0"
    )
    issue = compatibility.evaluate_plugin_compatibility(
        {"name": "demo", "version": "1.0.0", "engines": {"dsh": "^9.0.0"}},
        {"demo@1.0.0": ["0.3.0"]},
        "0.3.0",
    )
    assert issue is not None and issue["exempted"]
    with pytest.raises(ValueError, match="runtime"):
        compatibility.unmet_requirements({}, "bad")
    with pytest.raises(ValueError, match="name"):
        compatibility.evaluate_plugin_compatibility(
            {"engines": {"dsh": "^9"}}, {}, "0.3.0"
        )
    assert install_spec.dependency_spec("p", "1", "/tmp") == "p@1"


def test_operations_helpers_and_runner(tmp_path: Path) -> None:
    assert operations.registry_arguments(None) == []
    assert operations.registry_arguments("https://x/") == ["--registry=https://x/"]
    assert operations.anchor_path_spec("./x", "/work").startswith("/")
    assert operations.anchor_path_spec("file:./x", "/work").startswith("file:")
    assert operations.anchor_path_spec("pkg", "/work") == "pkg"
    assert operations.named_specs(["add", "a", "--save-exact", "b"]) == ["a", "b"]
    assert operations.direct_dependencies(
        {"dependencies": {"a": "1"}, "devDependencies": {"b": "2"}}
    ) == {"a": "1", "b": "2"}
    operations.save_manifest(str(tmp_path), {"dependencies": {"a": "1"}})
    assert (tmp_path / "package.json").stat().st_mode & 0o777 == 0o600

    manifest = {"dependencies": {"demo": "1.0.0"}, "dsh": {"bundle": {}}}
    (tmp_path / "installed" / "demo").mkdir(parents=True)
    (tmp_path / "node_modules" / "demo").mkdir(parents=True)
    (tmp_path / "node_modules" / "demo" / "package.json").write_text(
        json.dumps({"version": "1.0.0", "dsh": {"bundle": {}}}), encoding="utf-8"
    )
    assert (
        operations.expected_unmet(
            manifest, str(tmp_path), {"name": "demo", "version": "1.0.0"}
        )
        is None
    )
    assert (
        operations.expected_unmet(
            manifest, str(tmp_path), {"name": "demo", "version": "2"}
        )
        == "unexpected-package"
    )
    assert (
        operations.expected_unmet(
            manifest, str(tmp_path), {"name": "other", "version": "1"}
        )
        == "unexpected-package"
    )
    assert operations.expected_unmet(manifest, str(tmp_path), {}) == "invalid-spec"

    async def spawn(argv: Any, cwd: Any, env: Any, signal: Any = None) -> FakeProcess:
        return FakeProcess(0, b"ok", b"")

    result = asyncio.run(
        operations.run_profile_pnpm(
            str(tmp_path), ["add", "demo"], spawn=spawn, expected=None
        )
    )
    assert result["exitCode"] == 0 and result["output"] == "ok"
    result2 = asyncio.run(
        operations.run_profile_pnpm(
            str(tmp_path),
            ["add", "demo"],
            spawn=spawn,
            expected={"name": "demo", "version": "2"},
        )
    )
    assert result2["kind"] == "unexpected-package"

    async def failing(argv: Any, cwd: Any, env: Any, signal: Any = None) -> FakeProcess:
        return FakeProcess(1, b"ERR_PNPM_FETCH_404", b"")

    failed = asyncio.run(
        operations.run_profile_pnpm(str(tmp_path), ["add"], spawn=failing)
    )
    assert failed["kind"] == "not-found"

    viewed = asyncio.run(
        operations.view_profile_package("demo", ["version"], str(tmp_path), spawn=spawn)
    )
    assert viewed["exitCode"] == 0
    viewed2 = asyncio.run(
        operations.view_profile_package(
            "demo",
            ["version"],
            str(tmp_path),
            spawn=lambda *a, **k: asyncio.sleep(
                0, result=FakeProcess(0, b'{"version":"1"}', b"")
            ),
        )
    )
    assert viewed2["answer"]["version"] == "1"

    registry = asyncio.run(
        operations.read_profile_registry(
            str(tmp_path),
            spawn=lambda *a, **k: asyncio.sleep(
                0, result=FakeProcess(0, b"registry: https://x/\nhttps://x/\n", b"")
            ),
        )
    )
    assert registry == "https://x/"


def test_github_and_transport(tmp_path: Path) -> None:
    spec = {"kind": "git", "spec": "github:u/r#abc", "host": "github.com"}
    assert github_connection.is_github_spec(spec)
    assert not github_connection.is_github_spec({"kind": "registry"})
    assert github_connection.repository_url(spec) == "https://github.com/u/r"
    assert "user:pass" not in str(
        github_connection.scrubbed_argv("https://user:pass@github.com/u/r")
    )
    assert (
        asyncio.run(
            github_connection.check_github_connection(
                {"kind": "registry"}, str(tmp_path)
            )
        )
        is None
    )
    ok = asyncio.run(
        github_connection.check_github_connection(
            spec,
            str(tmp_path),
            spawn=lambda *a, **k: asyncio.sleep(0, result=FakeProcess(0, b"", b"")),
        )
    )
    assert ok is None
    bad = asyncio.run(
        github_connection.check_github_connection(
            spec,
            str(tmp_path),
            spawn=lambda *a, **k: asyncio.sleep(
                0, result=FakeProcess(1, b"", b"ENOTFOUND github.com")
            ),
        )
    )
    assert bad is not None and bad["kind"] == "network"

    custom = {
        "kind": "git",
        "spec": "https://git.example.com/u/r#abc",
        "host": "git.example.com",
    }
    assert git_transport.ssh_git_spec(custom) == "git+ssh://git@git.example.com/u/r#abc"
    assert git_transport.ssh_git_spec(spec) is None

    async def https_readable(*_a: Any, **_k: Any) -> FakeProcess:
        return FakeProcess(0)

    assert asyncio.run(
        git_transport.readable("url", str(tmp_path), spawn=https_readable)
    )

    async def ssh_only(*args: Any, **_k: Any) -> FakeProcess:
        return FakeProcess(0 if "ssh" in str(args[0]) else 1)

    assert asyncio.run(
        git_transport.choose_git_transport(custom, str(tmp_path), spawn=ssh_only)
    ).startswith("git+ssh://")


def test_bundle_detail_and_target(tmp_path: Path) -> None:
    bundle = tmp_path / "installed" / "demo"
    bundle.mkdir(parents=True)
    (bundle / "package.json").write_text(
        json.dumps({"name": "demo", "meta": {"detail": "docs/guide.md"}}),
        encoding="utf-8",
    )
    (bundle / "docs").mkdir()
    (bundle / "docs" / "guide.md").write_text("# Guide", encoding="utf-8")
    result = bundle_detail.read_bundle_document(str(tmp_path), "demo", "en")
    assert result["status"] == "found" and result["markdown"] == "# Guide"
    with pytest.raises(RemoteError, match="Invalid display language"):
        bundle_detail.language_chain("bad language")
    (bundle / "docs" / "a.png").write_bytes(b"png")
    image = bundle_detail.read_bundle_image(
        str(tmp_path), "demo", "docs/guide.md", "a.png"
    )
    assert image["mediaType"] == "image/png"

    target = official_install_target.official_bundle_install_target(
        "demo", str(tmp_path / "cli" / "package.json"), "0.3.0"
    )
    assert target["spec"] == "demo@0.3.0"
