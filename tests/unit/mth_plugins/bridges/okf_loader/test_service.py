"""Unit tests for OKF Session Catalog + Loader Tool service."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from animas import Context
from magitech_harness.session.events import ToolCallData
from magitech_harness.system_prompt.service import SystemPromptService
from magitech_harness.tools.native_executor import apply as apply_native_executor
from magitech_harness.tools.service import ToolService

from mth_plugins.bridges.okf_filesystem.service import OKFFilesystemProviderService
from mth_plugins.bridges.okf_loader.service import OKFSessionCatalogService
from mth_plugins.bridges.okf_registry.service import (
    OKFBundleInfo,
    OKFProviderRegistryService,
)

# Resolve the shared fixture from the repo layout (portable across machines).
FIXTURE = Path(__file__).resolve().parents[4] / "fixtures" / "okf_test_bundle"


async def test_okf_loader_registers_and_loads_bundle(tmp_path: Path) -> None:
    """OKF loader service registers catalog and serves bundle content via tool."""

    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    ctx.use(OKFProviderRegistryService)
    ctx.use(apply_native_executor)
    await ctx.flush()

    # Register test bundle in registry
    registry = ctx.okf_registry
    bundle_path = FIXTURE
    bundle_info = OKFBundleInfo(
        name="okf_test_bundle",
        title="OKF Test Bundle — Coding Standards",
        description="Test bundle derived from OKF v0.3 SPEC.md template examples.",
        path=bundle_path,
        okf_version="0.3",
    )
    registry.register_bundle(bundle_info)

    # Activate loader service
    ctx.use(OKFSessionCatalogService)
    await ctx.flush()
    service: OKFSessionCatalogService = ctx.okf_loader
    service.activate()

    # Verify catalog was injected
    rendered_prompt = await ctx.system_prompt.render()
    assert "<okf_bundles>" in rendered_prompt
    assert "<name>okf_test_bundle</name>" in rendered_prompt
    assert "OKF v0.3 SPEC.md template examples" in rendered_prompt

    # Call okf_bundle tool
    result = await ctx.tools.execute(
        ToolCallData(
            call_id="call-1",
            name="okf_bundle",
            arguments={"name": "okf_test_bundle"},
        )
    )

    # Verify output structure
    assert result.content.startswith('<okf_bundle_content name="okf_test_bundle">\n')
    assert "<resource_base>" in result.content
    assert '<trust_summary count="2">' in result.content
    # Match the platform's own path rendering (Windows uses backslashes).
    assert str(bundle_path.resolve()) in result.content

    # Verify bundle content is included
    index_content = (bundle_path / "index.md").read_text(encoding="utf-8")
    assert index_content in result.content


async def test_okf_loader_golden_fixture(tmp_path: Path) -> None:
    """OKF loader output matches golden fixture from OKF spec primary docs."""

    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    ctx.use(OKFProviderRegistryService)
    await ctx.flush()

    # Register test bundle
    registry = ctx.okf_registry
    bundle_path = FIXTURE
    bundle_info = OKFBundleInfo(
        name="okf_test_bundle",
        title="OKF Test Bundle — Coding Standards",
        description="Test bundle derived from OKF v0.3 SPEC.md template examples.",
        path=bundle_path,
        okf_version="0.3",
    )
    registry.register_bundle(bundle_info)

    # Activate loader service
    ctx.use(OKFSessionCatalogService)
    await ctx.flush()
    service: OKFSessionCatalogService = ctx.okf_loader
    service.activate()

    # Call okf_bundle tool
    result = await ctx.tools.execute(
        ToolCallData(
            call_id="call-1",
            name="okf_bundle",
            arguments={"name": "okf_test_bundle"},
        )
    )

    # Load golden fixture and substitute resolved resource_base path
    fixture_path = FIXTURE / "expected_okf_bundle_output.txt"
    expected = fixture_path.read_text(encoding="utf-8")
    expected = expected.replace("{RESOURCE_BASE}", str(bundle_path.resolve()))

    assert result.content == expected


async def test_okf_loader_unknown_bundle(tmp_path: Path) -> None:
    """okf_bundle tool returns error for unknown bundle name."""

    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    ctx.use(OKFProviderRegistryService)
    await ctx.flush()

    ctx.use(OKFSessionCatalogService)
    await ctx.flush()
    service: OKFSessionCatalogService = ctx.okf_loader
    service.activate()

    result = await ctx.tools.execute(
        ToolCallData(
            call_id="call-1",
            name="okf_bundle",
            arguments={"name": "nonexistent_bundle"},
        )
    )

    assert "Error: OKF bundle 'nonexistent_bundle' not found" in result.content


async def test_okf_filesystem_provider_discovery(tmp_path: Path) -> None:
    """OKF filesystem provider discovers bundles from knowledge directory."""

    ctx = Context()
    ctx.use(OKFProviderRegistryService)
    await ctx.flush()

    service = OKFFilesystemProviderService(ctx)
    discovered = service.discover_bundles(roots=[(FIXTURE.parent, "project")])

    assert "okf_test_bundle" in discovered

    # Verify bundle is registered in registry
    registry = ctx.okf_registry
    bundle = registry.get_bundle("okf_test_bundle")
    assert bundle is not None
    assert bundle.name == "okf_test_bundle"
    assert bundle.okf_version == "0.3"
    assert bundle.title == "OKF Test Bundle — Coding Standards"


async def test_okf_catalog_empty_and_no_system_prompt() -> None:
    ctx = Context()
    ctx.use(OKFProviderRegistryService)
    ctx.use(OKFSessionCatalogService)
    await ctx.flush()
    loader = ctx.okf_loader
    assert loader._build_catalog_xml() == ""
    loader._inject_catalog()  # empty catalog -> early return
    assert loader._prompt_disposer is None
    # Add a bundle but no SystemPromptService mounted -> no prompt section.
    ctx.okf_registry.register_bundle(
        OKFBundleInfo("b", "B", "d", Path("/tmp/b"), "0.3")
    )
    loader._inject_catalog()
    assert loader._prompt_disposer is None


async def test_okf_loader_no_tools_service() -> None:
    ctx = Context()
    ctx.use(OKFProviderRegistryService)
    await ctx.flush()
    loader = OKFSessionCatalogService(ctx)
    loader.mount_tools()  # no tools service -> early return
    assert loader._tool_disposers == []


async def test_okf_loader_tool_edge_cases(tmp_path: Path) -> None:
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    ctx.use(apply_native_executor)
    ctx.use(OKFProviderRegistryService)
    ctx.use(OKFSessionCatalogService)
    await ctx.flush()
    loader = ctx.okf_loader
    loader.mount_tools()
    ctx.okf_registry.register_bundle(
        OKFBundleInfo("empty", "E", "d", tmp_path / "no-index", "0.3")
    )
    r = await ctx.tools.execute(
        ToolCallData(call_id="c1", name="okf_bundle", arguments={"name": ""})
    )
    assert "non-empty" in r.content
    r = await ctx.tools.execute(
        ToolCallData(call_id="c2", name="okf_bundle", arguments={"name": "empty"})
    )
    assert "index.md not found" in r.content
    good = tmp_path / "good"
    good.mkdir()
    (good / "index.md").write_text("content")
    ctx.okf_registry.register_bundle(OKFBundleInfo("good", "G", "d", good, "0.3"))
    with patch.object(Path, "read_text", side_effect=OSError("denied")):
        r = await ctx.tools.execute(
            ToolCallData(call_id="c3", name="okf_bundle", arguments={"name": "good"})
        )
    assert "Failed to read" in r.content
    loader.mount_tools()  # clears previous disposers (line 88)
    loader._prompt_disposer = MagicMock()
    loader._cleanup()
    assert loader._prompt_disposer is None
    assert loader._tool_disposers == []


async def test_okf_bundle_result_includes_concept_map() -> None:
    """The loader result carries a routing map for every concept."""
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    ctx.use(OKFProviderRegistryService)
    await ctx.flush()
    registry = ctx.okf_registry
    registry.register_bundle(
        OKFBundleInfo(
            name="okf_test_bundle",
            title="Fixture",
            description="Fixture",
            path=FIXTURE,
            okf_version="0.3",
        )
    )
    ctx.use(OKFSessionCatalogService)
    await ctx.flush()
    ctx.okf_loader.activate()

    result = await ctx.tools.execute(
        ToolCallData(
            call_id="map", name="okf_bundle", arguments={"name": "okf_test_bundle"}
        )
    )
    assert '<concept_map count="2">' in result.content
    assert (
        "policies/cross-platform-path-assertions.md | cross-platform-path-assertions"
        in result.content
    )
    assert "references/okf-spec-md.md | okf-spec-md" in result.content


async def test_loader_refreshes_catalog_on_registry_change() -> None:
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    ctx.use(apply_native_executor)
    ctx.use(OKFProviderRegistryService)
    ctx.use(OKFSessionCatalogService)
    await ctx.flush()
    loader = ctx.okf_loader
    loader.activate()
    assert "<okf_bundles>" not in await ctx.system_prompt.render()

    ctx.okf_registry.register_bundle(
        OKFBundleInfo(
            "later", "Later", "Added after activation", Path("/tmp/later"), "0.3"
        )
    )
    rendered = await ctx.system_prompt.render()
    assert "<name>later</name>" in rendered

    ctx.okf_registry.bundles.clear()
    ctx.emit("okf/bundles-changed")
    assert "<okf_bundles>" not in await ctx.system_prompt.render()


async def test_okf_bundle_concepts_tool_returns_bodies() -> None:
    """The batch tool loads selected concept bodies in one call."""
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    ctx.use(apply_native_executor)
    ctx.use(OKFProviderRegistryService)
    await ctx.flush()
    ctx.okf_registry.register_bundle(
        OKFBundleInfo("fixture", "Fixture", "d", FIXTURE, "0.3")
    )
    ctx.use(OKFSessionCatalogService)
    await ctx.flush()
    ctx.okf_loader.activate()

    result = await ctx.tools.execute(
        ToolCallData(
            call_id="bodies",
            name="okf_bundle_concepts",
            arguments={"name": "fixture", "ids": ["okf-spec-md", "missing"]},
        )
    )
    assert not result.is_error
    assert '<okf_concept id="okf-spec-md"' in result.content
    assert "Open Knowledge Format v0.3 specification" in result.content
    assert "omitted: missing" in result.content


async def test_okf_bundle_concepts_error_paths() -> None:
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    ctx.use(apply_native_executor)
    ctx.use(OKFProviderRegistryService)
    ctx.use(OKFSessionCatalogService)
    await ctx.flush()
    ctx.okf_loader.activate()

    for args, expected in (
        ({"name": "", "ids": ["a"]}, "non-empty 'name'"),
        ({"name": "x", "ids": [123]}, "'ids' list"),
        ({"name": "missing", "ids": ["a"]}, "not found"),
    ):
        result = await ctx.tools.execute(
            ToolCallData(call_id="err", name="okf_bundle_concepts", arguments=args)
        )
        assert expected in result.content
