"""Unit tests for sandbox privilege ladder and containment."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from animas import Context
from magitech_harness.shell.service import ShellResult

from mth_plugins.sandbox.ladder import (
    PrivilegeLevel,
    PrivilegeViolationError,
    SandboxDiagnostics,
    SandboxMode,
    SandboxService,
    SandboxUnavailableError,
    build_bubblewrap_command,
    find_bubblewrap,
    sandbox_mode_level,
)


def test_sandbox_mode_level_maps_modes() -> None:
    assert sandbox_mode_level(SandboxMode.READ_ONLY).value == "readonly"
    assert sandbox_mode_level(SandboxMode.WORKSPACE_WRITE).value == "strict"
    assert sandbox_mode_level(SandboxMode.DANGER_FULL_ACCESS).value == "unrestricted"
    assert sandbox_mode_level("read-only").value == "readonly"


def test_find_bubblewrap(tmp_path: Path) -> None:
    # Custom existing file
    custom_bin = tmp_path / "custom-bwrap"
    custom_bin.write_text("#!/bin/sh\nexit 0\n")
    assert find_bubblewrap(str(custom_bin)) == str(custom_bin)

    # Custom non-existing file
    assert find_bubblewrap(str(tmp_path / "non-existent")) is None

    # Fallback to PATH lookup
    with patch("shutil.which", return_value="/usr/bin/bwrap"):
        assert find_bubblewrap() == "/usr/bin/bwrap"


def test_build_bubblewrap_command_modes(tmp_path: Path) -> None:
    # 1. STRICT mode with str command
    cmd_strict = build_bubblewrap_command(
        "/usr/bin/bwrap",
        "echo hello",
        cwd=tmp_path,
        level=PrivilegeLevel.STRICT,
    )
    assert "--die-with-parent" in cmd_strict
    assert "--unshare-net" in cmd_strict
    assert "--bind" in cmd_strict
    assert str(tmp_path.resolve()) in cmd_strict
    assert cmd_strict[-3:] == ["/bin/sh", "-c", "echo hello"]

    # 2. READONLY mode with cwd
    cmd_ro = build_bubblewrap_command(
        "/usr/bin/bwrap",
        ["ls", "-la"],
        cwd=tmp_path,
        level=PrivilegeLevel.READONLY,
    )
    assert "--ro-bind" in cmd_ro
    assert str(tmp_path.resolve()) in cmd_ro
    assert cmd_ro[-2:] == ["ls", "-la"]

    # 3. NETWORK mode without cwd
    cmd_net = build_bubblewrap_command(
        "/usr/bin/bwrap",
        "curl https://example.com",
        cwd=None,
        level=PrivilegeLevel.NETWORK,
    )
    assert "--unshare-net" not in cmd_net
    assert "/etc/resolv.conf" in cmd_net
    assert cmd_net[-3:] == ["/bin/sh", "-c", "curl https://example.com"]


def test_sandbox_service_diagnostics() -> None:
    ctx = Context()
    svc = SandboxService(
        ctx,
        {
            "max_level": PrivilegeLevel.STRICT,
            "enforce": False,
            "bwrap_path": "/usr/bin/bwrap",
        },
    )
    with patch("sys.platform", "linux"):
        svc.bwrap_path = "/usr/bin/bwrap"
        assert svc.is_available() is True
        diag = svc.get_diagnostics()
        assert isinstance(diag, SandboxDiagnostics)
        assert diag.isolation_backend == "bubblewrap"
        assert diag.bwrap_available is True

    with patch("sys.platform", "win32"):
        assert svc.is_available() is False
        diag_win = svc.get_diagnostics()
        assert diag_win.isolation_backend == "none"


def test_sandbox_validate_privilege() -> None:
    ctx = Context()
    svc = SandboxService(ctx, {"max_level": PrivilegeLevel.STRICT})

    # Valid: READONLY <= STRICT, STRICT <= STRICT
    svc.validate_privilege(PrivilegeLevel.READONLY)
    svc.validate_privilege(PrivilegeLevel.STRICT)

    # Verify NETWORK exceeds permitted budget
    with pytest.raises(PrivilegeViolationError, match="exceeds permitted maximum"):
        svc.validate_privilege(PrivilegeLevel.NETWORK)


def test_sandbox_wrap_command_unrestricted(tmp_path: Path) -> None:
    ctx = Context()
    svc = SandboxService(ctx, {"max_level": PrivilegeLevel.UNRESTRICTED})

    # String command
    assert svc.wrap_command("ls -la", cwd=tmp_path) == ["/bin/sh", "-c", "ls -la"]
    # List command
    assert svc.wrap_command(["ls", "-la"], cwd=tmp_path) == ["ls", "-la"]


def test_sandbox_wrap_command_unavailable_fallback_and_enforce(tmp_path: Path) -> None:
    ctx = Context()

    # Fallback mode (enforce=False)
    svc_fallback = SandboxService(
        ctx,
        {"max_level": PrivilegeLevel.STRICT, "enforce": False},
    )
    with patch.object(svc_fallback, "is_available", return_value=False):
        wrapped_str = svc_fallback.wrap_command("ls", cwd=tmp_path)
        assert wrapped_str == ["/bin/sh", "-c", "ls"]
        wrapped_list = svc_fallback.wrap_command(["echo", "hi"], cwd=tmp_path)
        assert wrapped_list == ["echo", "hi"]

    # Enforce mode (enforce=True) -> raises SandboxUnavailableError
    svc_enforce = SandboxService(
        ctx,
        {"max_level": PrivilegeLevel.STRICT, "enforce": True},
    )
    with (
        patch.object(svc_enforce, "is_available", return_value=False),
        pytest.raises(SandboxUnavailableError, match="Bubblewrap is unavailable"),
    ):
        svc_enforce.wrap_command("ls", cwd=tmp_path)


def test_sandbox_wrap_command_available(tmp_path: Path) -> None:
    ctx = Context()
    svc = SandboxService(ctx, {"max_level": PrivilegeLevel.STRICT})
    with patch.object(svc, "is_available", return_value=True):
        svc.bwrap_path = "/usr/bin/bwrap"
        cmd = svc.wrap_command("whoami", cwd=tmp_path)
        assert cmd[0] == "/usr/bin/bwrap"
        assert "whoami" in cmd[-1]


def test_sandbox_wrap_command_mode_read_only(tmp_path: Path) -> None:
    """mode=read-only maps to READONLY privilege (read-only bind)."""
    ctx = Context()
    svc = SandboxService(ctx, {"max_level": PrivilegeLevel.UNRESTRICTED})
    with patch.object(svc, "is_available", return_value=True):
        svc.bwrap_path = "/usr/bin/bwrap"
        cmd = svc.wrap_command("cat file", cwd=tmp_path, mode="read-only")
        assert cmd[0] == "/usr/bin/bwrap"
        assert "--ro-bind" in cmd
        assert "--unshare-net" in cmd


def test_sandbox_wrap_command_mode_workspace_write(tmp_path: Path) -> None:
    """mode=workspace-write maps to STRICT privilege (read/write bind)."""
    ctx = Context()
    svc = SandboxService(ctx, {"max_level": PrivilegeLevel.UNRESTRICTED})
    with patch.object(svc, "is_available", return_value=True):
        svc.bwrap_path = "/usr/bin/bwrap"
        cmd = svc.wrap_command("echo x > file", cwd=tmp_path, mode="workspace-write")
        assert cmd[0] == "/usr/bin/bwrap"
        assert "--bind" in cmd
        assert "--ro-bind" not in cmd


def test_sandbox_wrap_command_mode_danger_full_access(tmp_path: Path) -> None:
    """mode=danger-full-access maps to UNRESTRICTED (no sandboxing)."""
    ctx = Context()
    svc = SandboxService(ctx, {"max_level": PrivilegeLevel.UNRESTRICTED})
    cmd = svc.wrap_command("rm -rf /", cwd=tmp_path, mode="danger-full-access")
    assert cmd == ["/bin/sh", "-c", "rm -rf /"]


def test_sandbox_wrap_command_mode_takes_precedence_over_level(tmp_path: Path) -> None:
    """When both mode and level are given, mode wins."""
    ctx = Context()
    svc = SandboxService(ctx, {"max_level": PrivilegeLevel.UNRESTRICTED})
    with patch.object(svc, "is_available", return_value=True):
        svc.bwrap_path = "/usr/bin/bwrap"
        # level=UNRESTRICTED would short-circuit unwrapped; mode forces sandbox.
        cmd = svc.wrap_command(
            "cat file",
            cwd=tmp_path,
            level=PrivilegeLevel.UNRESTRICTED,
            mode="read-only",
        )
        assert cmd[0] == "/usr/bin/bwrap"
        assert "--ro-bind" in cmd


async def test_sandbox_run_success(tmp_path: Path) -> None:
    ctx = Context()
    svc = SandboxService(ctx, {"max_level": PrivilegeLevel.STRICT, "enforce": False})

    mock_shell = AsyncMock()
    mock_shell.run.return_value = ShellResult(
        exit_code=0,
        stdout="sandboxed output",
        stderr="",
        timed_out=False,
        aborted=False,
        bytes_read=16,
    )
    ctx._register_service("shell", mock_shell)

    res = await svc.run("echo hello", cwd=str(tmp_path))
    assert res.exit_code == 0
    assert res.stdout == "sandboxed output"
    mock_shell.run.assert_awaited_once()


async def test_sandbox_run_passes_mode_through(tmp_path: Path) -> None:
    """run() forwards mode to wrap_command."""
    ctx = Context()
    svc = SandboxService(
        ctx, {"max_level": PrivilegeLevel.UNRESTRICTED, "enforce": False}
    )
    with patch.object(svc, "is_available", return_value=True):
        svc.bwrap_path = "/usr/bin/bwrap"
        mock_shell = AsyncMock()
        mock_shell.run.return_value = ShellResult(
            exit_code=0,
            stdout="ok",
            stderr="",
            timed_out=False,
            aborted=False,
            bytes_read=2,
        )
        ctx._register_service("shell", mock_shell)

        await svc.run("cat file", cwd=str(tmp_path), mode="read-only")
        exec_str = mock_shell.run.await_args.args[0]
        assert "bwrap" in exec_str
        assert "--ro-bind" in exec_str

    ctx = Context()
    svc = SandboxService(ctx, {"max_level": PrivilegeLevel.STRICT, "enforce": False})

    # Mock ctx.shell
    mock_shell = AsyncMock()
    mock_shell.run.return_value = ShellResult(
        exit_code=0,
        stdout="sandboxed output",
        stderr="",
        timed_out=False,
        aborted=False,
        bytes_read=16,
    )
    ctx._register_service("shell", mock_shell)

    res = await svc.run("echo hello", cwd=str(tmp_path))
    assert res.exit_code == 0
    assert res.stdout == "sandboxed output"
    mock_shell.run.assert_awaited_once()


async def test_sandbox_run_no_shell_raises(tmp_path: Path) -> None:
    ctx = Context()
    svc = SandboxService(ctx, {"max_level": PrivilegeLevel.STRICT, "enforce": False})
    with pytest.raises(RuntimeError, match="No shell service mounted"):
        await svc.run("echo test", cwd=tmp_path)


def test_sandbox_enforce_defaults_to_true() -> None:
    """Sandbox enforcement is fail-closed by default (finding #13)."""
    svc = SandboxService(MagicMock())
    assert svc.enforce is True

    with pytest.raises(SandboxUnavailableError):
        # With enforce=True and no bwrap available, wrap_command fails loud.
        svc.bwrap_path = None
        svc.wrap_command(["echo", "hi"], level=PrivilegeLevel.STRICT)
