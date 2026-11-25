"""End-to-end test for OKF + Skills consumption components."""

from __future__ import annotations

import asyncio
from pathlib import Path

from animas import Context
from magitech_harness.session.events import ToolCallData
from magitech_harness.system_prompt.service import SystemPromptService
from magitech_harness.tools.native_executor import apply as apply_native_executor
from magitech_harness.tools.service import ToolService

from mth_components.bridges.okf_loader.service import OKFSessionCatalogService
from mth_components.bridges.okf_registry.service import (
    OKFBundleInfo,
    OKFProviderRegistryService,
)
from mth_components.bridges.skills_loader.service import SkillsSessionCatalogService
from mth_components.bridges.skills_registry.service import (
    SkillBundleInfo,
    SkillsProviderRegistryService,
)


async def test_e2e_okf_and_skills_consumption():
    """Full end-to-end test: OKF + Skills components work together."""

    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    ctx.use(OKFProviderRegistryService)
    ctx.use(SkillsProviderRegistryService)
    ctx.use(apply_native_executor)
    await ctx.flush()

    # Register OKF bundles from the repo-local fixture so the test is
    # self-contained and needs no sibling agents-home checkout in CI.
    # tests/e2e/test.py -> tests/e2e -> tests -> mth-components (repo root)
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    okf_coding_path = fixtures / "okf_test_bundle"
    okf_engineering_path = fixtures / "okf_test_bundle"

    registry = ctx.okf_registry
    registry.register_bundle(
        OKFBundleInfo(
            name="coding-standards",
            title="Cross-platform path assertions",
            description="Coding standards enforced during review.",
            path=okf_coding_path,
            okf_version="0.3",
        )
    )
    registry.register_bundle(
        OKFBundleInfo(
            name="engineering-standards",
            title="Engineering standards",
            description="Engineering standards enforced during review.",
            path=okf_engineering_path,
            okf_version="0.3",
        )
    )

    # Register test skill
    skills_registry = ctx.skills_registry
    skills_registry.register_skill(
        SkillBundleInfo(
            name="code-review",
            description="Review changes since a fixed point along two axes.",
            path=Path("/tmp/test-skill"),
            skill_file=Path("/tmp/test-skill/SKILL.md"),
            body="Test skill body content.",
            rank=0,
        )
    )

    # Activate both loaders
    ctx.use(OKFSessionCatalogService)
    ctx.use(SkillsSessionCatalogService)
    await ctx.flush()
    ctx.okf_loader.activate()
    ctx.skills_loader.activate()

    # Verify catalog injection
    rendered = await ctx.system_prompt.render()
    assert "<okf_bundles>" in rendered, "OKF catalog not injected"
    assert "<skills>" in rendered, "Skills catalog not injected"
    assert "<name>coding-standards</name>" in rendered
    assert "<name>engineering-standards</name>" in rendered
    assert "<name>code-review</name>" in rendered

    # Test okf_bundle tool
    result = await ctx.tools.execute(
        ToolCallData(
            call_id="call-1",
            name="okf_bundle",
            arguments={"name": "coding-standards"},
        )
    )
    assert '<okf_bundle_content name="coding-standards">' in result.content
    assert "<resource_base>" in result.content
    assert "OKF Test Bundle — Coding Standards" in result.content

    # Test skill tool
    result2 = await ctx.tools.execute(
        ToolCallData(
            call_id="call-2",
            name="skill",
            arguments={"name": "code-review"},
        )
    )
    assert "Test skill body content." in result2.content

    # Test unknown bundle
    result3 = await ctx.tools.execute(
        ToolCallData(
            call_id="call-3",
            name="okf_bundle",
            arguments={"name": "nonexistent"},
        )
    )
    assert "not found" in result3.content

    # Test unknown skill
    result4 = await ctx.tools.execute(
        ToolCallData(
            call_id="call-4",
            name="skill",
            arguments={"name": "nonexistent"},
        )
    )
    assert "not found" in result4.content

    print("=== ALL E2E TESTS PASSED ===")
    print(f"  OKF catalog injected: {'<okf_bundles>' in rendered}")
    print(f"  Skills catalog injected: {'<skills>' in rendered}")
    print(
        f"  okf_bundle tool works: {'Cross-platform path assertions' in result.content}"
    )
    print(f"  skill tool works: {'Test skill body content.' in result2.content}")
    print(f"  Unknown bundle error: {'not found' in result3.content}")
    print(f"  Unknown skill error: {'not found' in result4.content}")


if __name__ == "__main__":
    asyncio.run(test_e2e_okf_and_skills_consumption())
