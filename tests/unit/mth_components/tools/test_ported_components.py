"""Tests that the ported components register their tools."""

from __future__ import annotations

from pathlib import Path

import pytest
from animas import Context
from magitech_harness.session.events import ToolCallData
from magitech_harness.system_prompt.service import SystemPromptService
from magitech_harness.tools.service import ToolService

from mth_components.tools.code_analysis.component import apply as code_analysis
from mth_components.tools.dev_tools.component import apply as dev_tools
from mth_components.tools.dev_tools.component import apply as dev_tools_component
from mth_components.tools.fs_extra.component import apply as fs_extra
from mth_components.tools.fs_extra.component import apply as fs_extra_component
from mth_components.tools.verify_repo.component import apply as verify_repo
from mth_components.tools.verify_repo.verify import verification_commands
from mth_components.tools.web_search.component import apply as web_search


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("component", "names"),
    [
        (fs_extra, {"delete_file", "list_directory"}),
        (code_analysis, {"code_metrics", "find_duplicates", "generate_docs"}),
        (web_search, {"web_search"}),
        (verify_repo, {"verify_repo"}),
        (dev_tools, {"reload_module", "add_repo"}),
    ],
)
async def test_component_registers_tools(component: object, names: set[str]) -> None:
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    ctx.use(component)
    await ctx.flush()
    registered = {d["name"] for d in ctx.tools.definitions()}
    assert names <= registered


@pytest.mark.asyncio
async def test_registered_tool_handlers_execute(tmp_path: Path) -> None:
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    ctx.use(fs_extra_component)
    ctx.use(dev_tools_component)
    await ctx.flush()

    r = await ctx.tools.execute(
        ToolCallData(call_id="d", name="delete_file", arguments={"path": ""})
    )
    assert "required" in r.content
    r = await ctx.tools.execute(
        ToolCallData(
            call_id="l", name="list_directory", arguments={"path": str(tmp_path)}
        )
    )
    assert r.content == "(empty)"
    r = await ctx.tools.execute(
        ToolCallData(
            call_id="r",
            name="reload_module",
            arguments={"module_name": "x", "file_path": str(tmp_path / "no.py")},
        )
    )
    assert "has not been read" in r.content
    r = await ctx.tools.execute(
        ToolCallData(call_id="a", name="add_repo", arguments={"ref": "x"})
    )
    assert "Error" in r.content


def test_verification_commands_no_fence(tmp_path: Path) -> None:
    d = tmp_path / "docs" / "agents"
    d.mkdir(parents=True)
    (d / "checks.md").write_text("just prose\n")
    assert (
        verification_commands(tmp_path)
        == verification_commands.__globals__["DEFAULT_CHECKS"]
    )
