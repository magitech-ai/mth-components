"""Unit tests for the agent-facing plugin management tool."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from animas import Context
from magitech_harness import __version__
from magitech_harness.tools.schema import ToolArgsError
from magitech_harness.tools.service import ToolCallData, ToolResultData, ToolService

from mth_components.plugin_manager import PluginManager
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


def _make_ctx(root: Path) -> tuple[Context, ToolService, PluginManager]:
    ctx = Context()
    tools = ToolService(ctx)
    manager = PluginManager(ctx, {"root": str(root)})
    apply(ctx)
    return ctx, tools, manager


async def _call(tools: ToolService, action: str, **args: Any) -> ToolResultData:
    return await tools.execute(
        ToolCallData(
            call_id="c_pm",
            name="plugin_manager",
            arguments={"action": action, **args},
        )
    )


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
    manager.install_bundle(str(source))
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
    result = await _call(ctx.tools, "list_plugins")
    assert result.is_error is True

    _manager_ctx, tools, _manager = _make_ctx(tmp_path)
    definition = tools.get("plugin_manager")
    assert definition is not None
    aborted = type("S", (), {"aborted": True})()
    with pytest.raises(ToolArgsError):
        await definition["execute"](
            {"action": "list_plugins"}, type("E", (), {"signal": aborted})()
        )
    with pytest.raises(ToolArgsError):
        await _execute(_manager_ctx, {"action": "not-an-action"}, _Exec())
