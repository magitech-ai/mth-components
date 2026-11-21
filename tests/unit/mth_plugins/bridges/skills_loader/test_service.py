"""Unit tests for skills session catalog + loader tool service."""

from __future__ import annotations

from pathlib import Path

from anima import Context
from magitech_harness.session.events import ToolCallData
from magitech_harness.system_prompt.service import SystemPromptService
from magitech_harness.tools.native_executor import apply as apply_native_executor
from magitech_harness.tools.service import ToolService

from mth_plugins.bridges.skills_loader.service import (
    SkillsSessionCatalogService,
    _escape_xml,
)
from mth_plugins.bridges.skills_registry.service import (
    SkillBundleInfo,
    SkillInvocation,
    SkillsProviderRegistryService,
)


def _skill(name: str, model: bool = True, desc: str | None = None) -> SkillBundleInfo:
    return SkillBundleInfo(
        name=name,
        description=desc or f"desc {name}",
        path=Path("/tmp") / name,
        skill_file=Path("/tmp") / name / "SKILL.md",
        body=f"body of {name}",
        invocation=SkillInvocation(model_invocable=model),
    )


def _setup(with_prompt=True, with_tools=True):
    ctx = Context()
    if with_prompt:
        ctx.use(SystemPromptService)
    if with_tools:
        ctx.use(ToolService)
    ctx.use(SkillsProviderRegistryService)
    ctx.use(SkillsSessionCatalogService)
    return ctx


def test_escape_xml() -> None:
    assert _escape_xml("a&b<c>d\"e'f") == "a&amp;b&lt;c&gt;d&quot;e&apos;f"


async def test_catalog_empty_without_skills() -> None:
    ctx = _setup()
    await ctx.flush()
    loader = ctx.skills_loader
    assert loader._build_catalog_xml() == ""
    # Injecting with no skills is a no-op.
    loader._inject_catalog()
    assert loader._prompt_disposer is None


async def test_catalog_build_and_inject() -> None:
    ctx = _setup()
    ctx.use(apply_native_executor)
    await ctx.flush()
    ctx.skills_registry.register_skill(_skill("b", desc='desc <b> & "x"'))
    ctx.skills_registry.register_skill(_skill("a"))
    loader = ctx.skills_loader
    xml = loader._build_catalog_xml()
    assert xml.index("a") < xml.index("b")  # sorted
    assert "&lt;b&gt;" in xml
    assert "&amp;" in xml
    assert "&quot;" in xml
    assert "user-invoked" not in xml
    loader._inject_catalog()
    rendered = await ctx.system_prompt.render()
    assert "<skills>" in rendered
    assert "specialized capabilities" in rendered
    assert "skill({name})" in rendered


async def test_catalog_excludes_user_invoked() -> None:
    ctx = _setup()
    await ctx.flush()
    ctx.skills_registry.register_skill(_skill("user-only", model=False))
    loader = ctx.skills_loader
    assert loader._build_catalog_xml() == ""


async def test_inject_no_system_prompt() -> None:
    ctx = _setup(with_prompt=False)
    await ctx.flush()
    ctx.skills_registry.register_skill(_skill("a"))
    loader = ctx.skills_loader
    loader._inject_catalog()
    assert loader._prompt_disposer is None


async def test_mount_tools_and_handle() -> None:
    ctx = _setup()
    await ctx.flush()
    ctx.skills_registry.register_skill(_skill("good"))
    ctx.skills_registry.register_skill(_skill("hidden", model=False))
    loader = ctx.skills_loader
    loader.mount_tools()
    assert ctx.tools.get("skill") is not None
    good = await ctx.tools.execute(
        ToolCallData(call_id="c1", name="skill", arguments={"name": "good"})
    )
    assert good.content == "body of good"
    empty = await ctx.tools.execute(
        ToolCallData(call_id="c2", name="skill", arguments={"name": ""})
    )
    assert "non-empty" in empty.content
    missing = await ctx.tools.execute(
        ToolCallData(call_id="c3", name="skill", arguments={"name": "nope"})
    )
    assert "not found" in missing.content
    hidden = await ctx.tools.execute(
        ToolCallData(call_id="c4", name="skill", arguments={"name": "hidden"})
    )
    assert "not model-invocable" in hidden.content


async def test_mount_tools_no_tools_service() -> None:
    ctx = _setup(with_tools=False)
    await ctx.flush()
    loader = ctx.skills_loader
    loader.mount_tools()
    assert loader._tool_disposers == []


async def test_mount_tools_clears_previous() -> None:
    ctx = _setup()
    await ctx.flush()
    loader = ctx.skills_loader
    loader.mount_tools()
    loader.mount_tools()
    assert ctx.tools.get("skill") is not None


async def test_activate() -> None:
    ctx = _setup()
    await ctx.flush()
    ctx.skills_registry.register_skill(_skill("a"))
    loader = ctx.skills_loader
    loader.activate()
    assert loader._prompt_disposer is not None
    assert ctx.tools.get("skill") is not None


async def test_cleanup() -> None:
    ctx = _setup()
    await ctx.flush()
    ctx.skills_registry.register_skill(_skill("a"))
    loader = ctx.skills_loader
    loader.activate()
    assert loader._prompt_disposer is not None
    assert loader._tool_disposers
    loader._cleanup()
    assert loader._prompt_disposer is None
    assert loader._tool_disposers == []
