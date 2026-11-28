"""Remaining branch probes for plugin-manager helpers."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from mth_components.plugin_manager import (
    build_approval,
    bundle_detail,
    compatibility,
    git_transport,
    github_connection,
    official_install_target,
    operations,
    semver,
)


class SlowProcess:
    def __init__(self, code: int = 0) -> None:
        self.returncode = code
        self.pid = 999
        self.stdout = type(
            "S", (), {"read": lambda self, n: asyncio.sleep(0, result=b"")}
        )()
        self.stderr = type(
            "S", (), {"read": lambda self, n: asyncio.sleep(0, result=b"")}
        )()
        self.killed = False

    async def wait(self) -> int:
        await asyncio.sleep(10)
        return self.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(10)
        return b"", b""

    def kill(self) -> None:
        self.killed = True


def test_semver_and_compatibility_edges() -> None:
    assert semver.compare("1.0.0-alpha.2", "1.0.0-alpha.1") == 1
    assert semver.compare("1.0.0-2", "1.0.0-alpha") == -1
    assert semver.compare("1.0.0-alpha.1", "1.0.0-alpha") == 1
    assert not semver.satisfies("1.0.0", ">=bad")
    with pytest.raises(ValueError, match="version"):
        compatibility.evaluate_plugin_compatibility(
            {"engines": {"dsh": "^9"}, "name": "x"}, {}, "0.3.0"
        )


def test_build_approval_remaining(tmp_path: Path) -> None:
    assert build_approval._carries_credential("p@git+https://u:p@host/x") is True
    assert build_approval._carries_credential("p@git+git://host/x") is False
    assert build_approval._carries_credential("p@git+ssh://git@host/x") is False
    assert build_approval._carries_credential("p@git+http://host/x") is False
    workspace = tmp_path / "pnpm-workspace.yaml"
    workspace.write_text("allowBuilds: {}", encoding="utf-8")
    build_approval._set_entries(str(tmp_path), {"x": "true"})
    assert "allowBuilds:" in workspace.read_text(encoding="utf-8")
    workspace.write_text("top: 1\nallowBuilds:\n  a: true\n", encoding="utf-8")
    build_approval._set_entries(str(tmp_path), {"b": "false"})
    assert "b: false" in workspace.read_text(encoding="utf-8")


def test_bundle_detail_remaining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "installed" / "demo"
    bundle.mkdir(parents=True)
    (bundle / "package.json").write_text("{}", encoding="utf-8")
    assert bundle_detail.read_bundle_document(str(tmp_path), "demo", "en") == {
        "status": "none"
    }

    class ErrorResponse:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            raise bundle_detail.httpx.HTTPError("network")

    monkeypatch.setattr(bundle_detail.httpx, "get", ErrorResponse)
    assert bundle_detail.read_bundle_document(
        str(tmp_path), "demo", "en", sources=["https://bad/"]
    ) == {"status": "none"}
    with pytest.raises(Exception, match="unavailable"):
        bundle_detail.read_bundle_image(
            str(tmp_path), "demo", "x.md", "a.png", sources=["https://bad/"]
        )

    class BadResponse:
        status_code = 500
        content = b""

        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

    monkeypatch.setattr(bundle_detail.httpx, "get", BadResponse)
    assert bundle_detail.read_bundle_document(
        str(tmp_path), "demo", "en", sources=["https://bad/"]
    ) == {"status": "none"}
    monkeypatch.undo()

    # CDN-local image path with a successful response.
    class OkResponse:
        status_code = 200
        content = b"img"

        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

    monkeypatch.setattr(bundle_detail.httpx, "get", OkResponse)
    assert (
        bundle_detail.read_bundle_image(
            str(tmp_path), "demo", "x.md", "remote.png", sources=["https://cdn/"]
        )["bytes"]
        == b"img"
    )


def test_git_and_github_remaining(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        git_transport.ssh_git_spec({"kind": "git", "spec": "http://[bad/u"})

    async def timeout_spawn(*_a: Any, **_k: Any) -> Any:
        return SlowProcess()

    assert (
        asyncio.run(
            git_transport.readable(
                "x", str(tmp_path), timeout_ms=1, spawn=timeout_spawn
            )
        )
        is False
    )

    signal = type("S", (), {"aborted": True})()
    custom = {
        "kind": "git",
        "spec": "https://git.example.com/u/r#abc",
        "host": "git.example.com",
    }
    assert (
        asyncio.run(
            git_transport.choose_git_transport(
                custom,
                str(tmp_path),
                spawn=lambda *a, **k: asyncio.sleep(0, result=SlowProcess(1)),
                signal=signal,
            )
        )
        == custom["spec"]
    )

    async def timeout_github(*_a: Any, **_k: Any) -> Any:
        return SlowProcess()

    failed = asyncio.run(
        github_connection.check_github_connection(
            {"kind": "git", "spec": "github:u/r#x", "host": "github.com"},
            str(tmp_path),
            timeout_ms=1,
            spawn=timeout_github,
        )
    )
    assert failed is not None and failed["timedOut"]

    async def broken_spawn(*_a: Any, **_k: Any) -> Any:
        raise OSError("missing")

    failed2 = asyncio.run(
        github_connection.check_github_connection(
            {"kind": "git", "spec": "github:u/r#x", "host": "github.com"},
            str(tmp_path),
            spawn=broken_spawn,
        )
    )
    assert failed2 is not None and failed2["exitCode"] == 127


def test_official_target_remaining(tmp_path: Path) -> None:
    anchor = tmp_path / "cli" / "package.json"
    anchor.parent.mkdir(parents=True)
    anchor.write_text(json.dumps({"dependencies": {"dsh": "1.0.0"}}), encoding="utf-8")
    assert (
        official_install_target.official_bundle_install_target(
            "demo", str(anchor), "0.3.0"
        )["spec"]
        == "demo@0.3.0"
    )


def test_operations_remaining(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert operations._read_text(tmp_path / "absent") is None
    assert operations._tail("abcd", 2) == ("cd", True)
    captured: list[str] = []

    class Stream:
        def __init__(self) -> None:
            self.reader: Any = None

        async def read(self, _size: int) -> bytes:
            return await self.reader.read(4096)

    async def capture() -> None:
        stream = Stream()
        stream.reader = asyncio.StreamReader()
        stream.reader.feed_data(b"hello")
        stream.reader.feed_eof()
        await operations._capture(stream, captured, captured.append)

    asyncio.run(capture())
    assert captured == ["hello", "hello"]

    async def broken_spawn(*_a: Any, **_k: Any) -> Any:
        raise OSError("no pnpm")

    broken = asyncio.run(
        operations.run_profile_pnpm(str(tmp_path), ["add"], spawn=broken_spawn)
    )
    assert broken["exitCode"] == 127

    async def timeout_spawn(*_a: Any, **_k: Any) -> Any:
        return SlowProcess()

    timed = asyncio.run(
        operations.run_profile_pnpm(
            str(tmp_path), ["add"], spawn=timeout_spawn, idle_timeout_ms=1
        )
    )
    assert timed["timedOut"]

    monkeypatch.setattr(operations.os, "name", "nt")
    process = type(
        "P", (), {"pid": 1, "kill": lambda self: setattr(self, "killed", True)}
    )()
    operations._kill(process)
    assert process.killed
    monkeypatch.undo()

    manifest_path = tmp_path / "package.json"
    manifest_path.write_text("{}", encoding="utf-8")
    operations._restore(manifest_path, None)
    assert not manifest_path.exists()

    assert (
        operations.expected_unmet(
            {"dependencies": {"x": "1"}}, str(tmp_path), {"name": "x", "version": "1"}
        )
        == "unexpected-package"
    )
    manifest_path.write_text(json.dumps({"dependencies": {"x": "1"}}), encoding="utf-8")
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "package.json").write_text(
        json.dumps({"version": "2", "dsh": {"bundle": {}}}), encoding="utf-8"
    )
    assert (
        operations.expected_unmet(
            {"dependencies": {"x": "1"}}, str(tmp_path), {"name": "x", "version": "1"}
        )
        == "unexpected-package"
    )
    (tmp_path / "node_modules" / "x" / "package.json").write_text(
        json.dumps({"version": "1", "dsh": {}}), encoding="utf-8"
    )
    assert (
        operations.expected_unmet(
            {"dependencies": {"x": "1"}}, str(tmp_path), {"name": "x", "version": "1"}
        )
        == "not-bundle"
    )

    async def bad_view(*_a: Any, **_k: Any) -> Any:
        return SlowProcess()

    view = asyncio.run(
        operations.view_profile_package(
            "x", ["version"], str(tmp_path), timeout_ms=1, spawn=bad_view
        )
    )
    assert view["timedOut"] and view["exitCode"] is None
    view2 = asyncio.run(
        operations.view_profile_package(
            "x",
            ["version"],
            str(tmp_path),
            spawn=lambda *a, **k: asyncio.sleep(
                0,
                result=type(
                    "P",
                    (),
                    {
                        "returncode": 0,
                        "communicate": lambda self: asyncio.sleep(
                            0, result=(b"not json", b"")
                        ),
                        "kill": lambda self: None,
                        "wait": lambda self: asyncio.sleep(0, result=0),
                    },
                )(),
            ),
        )
    )
    assert view2["answer"] is None

    empty = asyncio.run(
        operations.read_profile_registry(
            str(tmp_path),
            spawn=lambda *a, **k: asyncio.sleep(
                0,
                result=type(
                    "P",
                    (),
                    {
                        "communicate": lambda self: asyncio.sleep(0, result=(b"", b"")),
                        "kill": lambda self: None,
                        "wait": lambda self: asyncio.sleep(0, result=0),
                    },
                )(),
            ),
        )
    )
    assert empty is None
    timed_registry = asyncio.run(
        operations.read_profile_registry(str(tmp_path), timeout_ms=1, spawn=bad_view)
    )
    assert timed_registry is None

    async def missing_spawn(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("pnpm")

    assert (
        asyncio.run(
            operations.read_profile_registry(str(tmp_path), spawn=missing_spawn)
        )
        is None
    )


def test_last_helper_probes(tmp_path: Path) -> None:
    assert semver.compare("1.0.0-alpha.beta", "1.0.0-alpha.1") == 1
    assert semver.compare("1.0.0-alpha.1", "1.0.0-alpha.beta") == -1

    assert build_approval._carries_credential("p@git+http://[bad/x") is False
    workspace = tmp_path / "pnpm-workspace.yaml"
    workspace.write_text("other: 1")
    build_approval._set_entries(str(tmp_path), {"x": "true"})
    assert "allowBuilds:" in workspace.read_text(encoding="utf-8")

    assert compatibility.evaluate_plugin_compatibility({}, {}, "0.3.0") is None

    async def https_ok(*_a: Any, **_k: Any) -> Any:
        return type(
            "P", (), {"returncode": 0, "wait": lambda self: asyncio.sleep(0, result=0)}
        )()

    spec = {
        "kind": "git",
        "spec": "https://git.example.com/u/r#abc",
        "host": "git.example.com",
    }
    assert (
        asyncio.run(
            git_transport.choose_git_transport(spec, str(tmp_path), spawn=https_ok)
        )
        == spec["spec"]
    )

    anchor = tmp_path / "apps" / "cli" / "package.json"
    anchor.parent.mkdir(parents=True)
    anchor.write_text(
        json.dumps(
            {"name": "@deepseek-ai/dsh", "dependencies": {"dsh": "workspace:*"}}
        ),
        encoding="utf-8",
    )
    root = tmp_path
    (root / "package.json").write_text(
        json.dumps({"name": "@deepseek-ai/dsh-root"}), encoding="utf-8"
    )
    package = root / "packages" / "boot" / "demo"
    package.mkdir(parents=True)
    (package / "package.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="unique package"):
        official_install_target.official_bundle_install_target(
            "demo", str(anchor), "0.3.0"
        )
    (package / "package.json").write_text(
        json.dumps({"name": "demo", "version": "0.3.0", "dsh": {}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="declares no bundle"):
        official_install_target.official_bundle_install_target(
            "demo", str(anchor), "0.3.0"
        )

    result = asyncio.run(
        operations.run_profile_pnpm(
            str(tmp_path), ["-c", "pass"], command=sys.executable, timeout_ms=5000
        )
    )
    assert result["exitCode"] == 0

    assert bundle_detail.read_bundle_document(str(tmp_path), "nodetail", "en") == {
        "status": "none"
    }


def test_final_branch_gaps(tmp_path: Path) -> None:
    # Both-prerelease string comparison.
    assert semver.compare("1.0.0-alpha.beta", "1.0.0-alpha.gamma") == -1

    workspace = tmp_path / "pnpm-workspace.yaml"
    workspace.write_text("other: 1\n", encoding="utf-8")
    build_approval._set_entries(str(tmp_path), {"y": "true"})

    # No local document and no CDN sources.
    assert bundle_detail.read_bundle_document(str(tmp_path), "none", "en") == {
        "status": "none"
    }
    assert bundle_detail._local_detail_file(tmp_path, {}, "en") is None

    # Successful expected check returns without an error.
    result = asyncio.run(
        operations.run_profile_pnpm(
            str(tmp_path), ["-c", "pass"], command=sys.executable, expected=None
        )
    )
    assert result["exitCode"] == 0


def test_absolute_final_probes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = tmp_path / "installed" / "broken"
    bundle.mkdir(parents=True)
    (bundle / "package.json").write_text("not a mapping", encoding="utf-8")
    assert bundle_detail._meta(bundle) == {}
    (bundle / "package.json").write_text(
        json.dumps({"meta": {"detail": "missing.md"}}), encoding="utf-8"
    )
    assert bundle_detail.read_bundle_document(str(tmp_path), "broken", "en") == {
        "status": "none"
    }

    class BadStatus:
        status_code = 404
        content = b""

        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

    monkeypatch.setattr(bundle_detail.httpx, "get", BadStatus)
    with pytest.raises(Exception, match="unavailable"):
        bundle_detail.read_bundle_image(
            str(tmp_path), "broken", "x.md", "a.png", sources=["https://cdn/"]
        )
    monkeypatch.undo()

    async def fail(*_a: Any, **_k: Any) -> Any:
        out = asyncio.StreamReader()
        err = asyncio.StreamReader()
        out.feed_eof()
        err.feed_eof()
        return type(
            "P",
            (),
            {
                "pid": 1,
                "returncode": 1,
                "stdout": out,
                "stderr": err,
                "wait": lambda self: asyncio.sleep(0, result=1),
                "kill": lambda self: None,
            },
        )()

    result = asyncio.run(
        operations.run_profile_pnpm(
            str(tmp_path),
            ["add"],
            spawn=fail,
            expected={"name": "x", "version": "1"},
        )
    )
    assert result["exitCode"] == 1


def test_zero_remaining_branches(tmp_path: Path) -> None:
    bundle = tmp_path / "installed" / "nondict"
    bundle.mkdir(parents=True)
    (bundle / "package.json").write_text("not a mapping", encoding="utf-8")
    assert bundle_detail._meta(bundle) == {}

    async def ok(*_a: Any, **_k: Any) -> Any:
        out = asyncio.StreamReader()
        err = asyncio.StreamReader()
        out.feed_eof()
        err.feed_eof()
        return type(
            "P",
            (),
            {
                "pid": 1,
                "returncode": 0,
                "stdout": out,
                "stderr": err,
                "wait": lambda self: asyncio.sleep(0, result=0),
                "kill": lambda self: None,
            },
        )()

    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"x": "1"}}), encoding="utf-8"
    )
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "package.json").write_text(
        json.dumps({"version": "1", "dsh": {"bundle": {}}}), encoding="utf-8"
    )
    result = asyncio.run(
        operations.run_profile_pnpm(
            str(tmp_path),
            ["add"],
            spawn=ok,
            expected={"name": "x", "version": "1"},
        )
    )
    assert result["exitCode"] == 0


@pytest.mark.asyncio
async def test_pnpm_cancel_watcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = asyncio.StreamReader()
    err = asyncio.StreamReader()
    out.feed_eof()
    err.feed_eof()
    killed: list[bool] = []

    class Process:
        pid = 4242
        returncode = 0
        stdout = out
        stderr = err

        async def wait(self) -> int:
            await asyncio.sleep(0.03)
            return 0

        def kill(self) -> None:
            killed.append(True)

    class Signal:
        aborted = False

    signal = Signal()

    async def abort() -> None:
        await asyncio.sleep(0)
        signal.aborted = True

    async def spawn(*_a: Any, **_k: Any) -> Process:
        return Process()

    monkeypatch.setattr(operations, "_kill", lambda process: killed.append(True))
    aborter = asyncio.create_task(abort())
    result = await operations.run_profile_pnpm(
        str(tmp_path), ["add"], spawn=spawn, signal=signal
    )
    await aborter
    assert killed and result["timedOut"] is True


@pytest.mark.asyncio
async def test_pnpm_log_is_private(tmp_path: Path) -> None:
    out = asyncio.StreamReader()
    err = asyncio.StreamReader()
    out.feed_eof()
    err.feed_eof()

    async def spawn(*_a: Any, **_k: Any) -> Any:
        return type(
            "P",
            (),
            {
                "pid": 7,
                "returncode": 0,
                "stdout": out,
                "stderr": err,
                "wait": lambda self: asyncio.sleep(0, result=0),
                "kill": lambda self: None,
            },
        )()

    result = await operations.run_profile_pnpm(str(tmp_path), ["add"], spawn=spawn)
    assert result["exitCode"] == 0
    log = next(tmp_path.glob(".plugin-manager/logs/*/pnpm.log"))
    assert log.stat().st_mode & 0o777 == 0o600


def test_watch_cancel_uses_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        operations.os, "killpg", lambda pid, sig: killed.append((pid, sig))
    )
    process = type("P", (), {"pid": 987654, "kill": lambda self: None})()
    signal = type("S", (), {"aborted": True})()
    asyncio.run(operations._watch_cancel(process, signal))
    assert killed == [(987654, 9)]
