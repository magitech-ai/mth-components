"""Unit tests for the agent-facing plugin management tool."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from animas import Context
from magitech_harness import __version__
from magitech_harness.tools.schema import ToolArgsError
from magitech_harness.tools.service import ToolService

from mth_components.plugin_manager import PluginManager
from mth_components.tools.plugin_manager import component as component_module
from mth_components.tools.plugin_manager.component import (
    ACTIONS,
    _execute,
    apply,
    with_next_step,
)


class _FakeSignal:
    aborted = False


class _Exec:
    signal = _FakeSignal()
    agent = type("A", (), {"session": object()})()
    call_id = "call-1"


class _Result:
    def __init__(self, is_error: bool, content: str) -> None:
        self.is_error = is_error
        self.content = content


_CURRENT_CTX: Context | None = None


@pytest.fixture(autouse=True)
def _approved(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(*_a: Any, **_k: Any) -> str:
        return "allowed-once"

    monkeypatch.setattr(component_module, "approve_escalation", fake)


def _make_ctx(root: Path) -> tuple[Context, ToolService, PluginManager]:
    global _CURRENT_CTX  # noqa: PLW0603
    ctx = Context()
    tools = ToolService(ctx)
    manager = PluginManager(ctx, {"root": str(root)})
    ctx.provide("approval", object())
    ctx.provide(
        "sandboxPolicy",
        type("P", (), {"resolve": lambda self, session: {"mode": "workspace-write"}})(),
    )
    apply(ctx)
    _CURRENT_CTX = ctx
    return ctx, tools, manager


async def _call(tools: ToolService, action: str, **args: Any) -> _Result:
    assert _CURRENT_CTX is not None
    try:
        content = await _execute(_CURRENT_CTX, {"action": action, **args}, _Exec())
        return _Result(False, content)
    except ToolArgsError as error:
        return _Result(True, str(error))


def test_next_step_tables() -> None:
    assert "nextStep" not in with_next_step({"application": "applied"})
    assert "nothing changed" in with_next_step({"application": "cancelled"})["nextStep"]
    assert "overrides" in with_next_step({"application": "overridden"})["nextStep"]
    assert (
        "restart"
        in with_next_step(
            {"application": "restart-required", "restartReason": "no-hmr"}
        )["nextStep"]
    )
    assert (
        "list_plugins"
        in with_next_step(
            {"application": "failed", "error": {"code": "unknown-plugin"}}
        )["nextStep"]
    )
    assert (
        "registry"
        in with_next_step(
            {
                "application": "failed",
                "error": {"code": "operation-error"},
                "packageResult": {"kind": "network"},
            }
        )["nextStep"]
    )
    assert with_next_step({"application": "failed", "error": {}})["nextStep"]
    assert len(ACTIONS) == 8


@pytest.mark.asyncio
async def test_tool_registration_and_lists(tmp_path: Path) -> None:
    _client_client_ctx, tools, manager = _make_ctx(tmp_path)
    tool = tools.get("plugin_manager")
    assert tool is not None and "action" in tool["parameters"]["properties"]

    result = await _call(tools, "list_plugins")
    assert result.is_error is False
    assert json.loads(result.content)["entries"] == []

    source = tmp_path / "src"
    source.mkdir()
    (source / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "version": "1.0.0",
                "dsh": {"bundle": {"rows": [{"id": "r", "module": "m"}]}},
            }
        ),
        encoding="utf-8",
    )
    await manager.install_bundle(str(source))
    listed = json.loads((await _call(tools, "list_bundles")).content)
    assert listed["total"] == 1 and listed["entries"][0]["name"] == "demo"
    page = json.loads((await _call(tools, "list_bundles", offset=0, limit=1)).content)
    assert page["nextOffset"] is None

    bad = await _call(tools, "list_bundles", offset=-1)
    assert bad.is_error is True


@pytest.mark.asyncio
async def test_tool_management_actions(tmp_path: Path) -> None:
    _client_client_ctx, tools, _manager = _make_ctx(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    (source / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "version": "1.0.0",
                "dsh": {"bundle": {"rows": [{"id": "r", "module": "m"}]}},
            }
        ),
        encoding="utf-8",
    )

    installed = json.loads(
        (await _call(tools, "install_bundle", target=str(source))).content
    )
    assert installed["bundle"] == "demo" and "restart" in installed["nextStep"]
    plugins = json.loads((await _call(tools, "list_plugins")).content)["entries"]
    row_id = plugins[0]["entryId"]
    assert (
        json.loads(
            (await _call(tools, "set_plugin", target=row_id, enabled=False)).content
        )["application"]
        == "restart-required"
    )
    assert (
        json.loads(
            (await _call(tools, "set_bundle", target="demo", enabled=False)).content
        )["application"]
        == "restart-required"
    )
    exemptions = json.loads((await _call(tools, "list_version_exemptions")).content)
    assert exemptions["runtimeVersion"] == __version__
    assert (
        json.loads(
            (
                await _call(
                    tools,
                    "set_version_exemption",
                    target="demo@1.0.0",
                    runtimeVersion="0.3.0",
                    enabled=True,
                    acceptRisk=True,
                )
            ).content
        )["application"]
        == "restart-required"
    )
    assert (
        json.loads((await _call(tools, "remove_bundle", target="demo")).content)[
            "application"
        ]
        == "restart-required"
    )

    for action, args in (
        ("install_bundle", {}),
        ("set_plugin", {"target": "demo"}),
        ("set_bundle", {"target": "demo"}),
        ("set_version_exemption", {"target": "x", "runtimeVersion": "1"}),
        ("remove_bundle", {}),
        ("unknown", {}),
    ):
        assert (await _call(tools, action, **args)).is_error is True


@pytest.mark.asyncio
async def test_tool_guards(tmp_path: Path) -> None:
    ctx = Context()
    ToolService(ctx)
    apply(ctx)
    with pytest.raises(ToolArgsError, match="approval service"):
        await _execute(ctx, {"action": "list_plugins"}, _Exec())

    approved_ctx = Context()
    ToolService(approved_ctx)
    apply(approved_ctx)
    approved_ctx.provide("approval", object())
    with pytest.raises(ToolArgsError, match="not mounted"):
        await _execute(approved_ctx, {"action": "list_plugins"}, _Exec())

    _manager_ctx, tools, _manager = _make_ctx(tmp_path)
    definition = tools.get("plugin_manager")
    assert definition is not None
    aborted = type("S", (), {"aborted": True})()
    with pytest.raises(ToolArgsError, match="cancelled"):
        await _execute(
            _manager_ctx,
            {"action": "list_plugins"},
            type("E", (), {"agent": _Exec.agent, "signal": aborted})(),
        )
    with pytest.raises(ToolArgsError):
        await definition["execute"](
            {"action": "list_plugins"}, type("E", (), {"signal": aborted})()
        )
    with pytest.raises(ToolArgsError):
        await _execute(_manager_ctx, {"action": "not-an-action"}, _Exec())


@pytest.mark.asyncio
async def test_escalation_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[dict[str, Any]] = []

    async def fake_escalation(request: dict[str, Any], approval: dict[str, Any]) -> str:
        called.append(request)
        return "allowed-once"

    monkeypatch.setattr(component_module, "approve_escalation", fake_escalation)
    ctx = Context()
    ToolService(ctx)
    PluginManager(ctx, {"root": str(tmp_path)})
    apply(ctx)
    ctx.provide("approval", object())
    ctx.provide(
        "sandboxPolicy",
        type("P", (), {"resolve": lambda self, session: {"mode": "workspace-write"}})(),
    )
    exec_ctx = type(
        "E",
        (),
        {
            "agent": type("A", (), {"session": object()})(),
            "call_id": "call-1",
            "callId": "call-1",
            "signal": _Exec.signal,
        },
    )()
    await component_module._execute(ctx, {"action": "list_plugins"}, exec_ctx)
    assert called and called[0]["requestedMode"] == "danger-full-access"
