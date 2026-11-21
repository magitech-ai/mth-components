"""Unit tests for OKF filesystem provider service."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from anima import Context

from mth_plugins.bridges.okf_filesystem.service import (
    OKFFilesystemProviderService,
    _parse_okf_frontmatter,
)
from mth_plugins.bridges.okf_registry.service import OKFProviderRegistryService


def _make_bundle(root: Path, name: str, frontmatter: str | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    fm = frontmatter or (
        '---\nokf_version: "0.3"\ntitle: My Title\n'
        "description: My Description\n---\n\n# Body\n"
    )
    (d / "index.md").write_text(fm, encoding="utf-8")
    return d


def _setup(ctx: Context):
    ctx.use(OKFProviderRegistryService)
    ctx.use(OKFFilesystemProviderService)


def test_parse_frontmatter_valid() -> None:
    result = _parse_okf_frontmatter(
        '---\nokf_version: "0.3"\ntitle: "T"\ndescription: D\n---\n\nbody'
    )
    assert result == {"okf_version": "0.3", "title": "T", "description": "D"}


def test_parse_frontmatter_invalid() -> None:
    assert _parse_okf_frontmatter("no frontmatter") == {}
    assert _parse_okf_frontmatter("---\nno closing") == {}
    # Lines without colons / comments / other keys are skipped.
    result = _parse_okf_frontmatter(
        "---\n# comment\nno colon here\nokf_version: 0.3\ntags: [a, b]\n---\n"
    )
    assert result == {"okf_version": "0.3"}


async def test_discover_bundles(tmp_path: Path) -> None:
    ctx = Context()
    _setup(ctx)
    await ctx.flush()
    svc = ctx.okf_filesystem
    _make_bundle(tmp_path, "bundle-b")
    _make_bundle(tmp_path, "bundle-a")
    discovered = svc.discover_bundles(root=tmp_path)
    assert discovered == ["bundle-a", "bundle-b"]
    registry = ctx.okf_registry
    assert registry.get_bundle("bundle-a") is not None
    assert registry.get_bundle("bundle-a").title == "My Title"
    assert registry.get_bundle("bundle-a").description == "My Description"


async def test_discover_skips_non_bundles(tmp_path: Path) -> None:
    ctx = Context()
    _setup(ctx)
    await ctx.flush()
    svc = ctx.okf_filesystem
    # A directory without index.md
    (tmp_path / "noindex").mkdir()
    # A file (not directory)
    (tmp_path / "afile.txt").write_text("x")
    # index.md without okf_version
    d = tmp_path / "noversion"
    d.mkdir()
    (d / "index.md").write_text("---\ntitle: T\n---\n")
    assert svc.discover_bundles(root=tmp_path) == []


async def test_discover_unreadable_index(tmp_path: Path) -> None:
    ctx = Context()
    _setup(ctx)
    await ctx.flush()
    svc = ctx.okf_filesystem
    _make_bundle(tmp_path, "bundle-x")
    with patch.object(Path, "read_text", side_effect=OSError("denied")):
        assert svc.discover_bundles(root=tmp_path) == []


async def test_discover_duplicate_loses_registration(tmp_path: Path) -> None:
    """When registry rejects a bundle it is not added to discovered list."""
    ctx = Context()
    _setup(ctx)
    await ctx.flush()
    svc = ctx.okf_filesystem
    _make_bundle(tmp_path, "bundle-dup")
    # First discovery registers it.
    assert svc.discover_bundles(root=tmp_path) == ["bundle-dup"]
    # Second discovery: registry tie => rejected => not in discovered.
    assert svc.discover_bundles(root=tmp_path) == []


async def test_discover_missing_root(tmp_path: Path) -> None:
    ctx = Context()
    _setup(ctx)
    await ctx.flush()
    svc = ctx.okf_filesystem
    assert svc.discover_bundles(root=tmp_path / "missing") == []


async def test_default_root(tmp_path: Path, monkeypatch) -> None:
    ctx = Context()
    _setup(ctx)
    await ctx.flush()
    svc = ctx.okf_filesystem
    with patch("mth_plugins.bridges.okf_filesystem.service.Path") as mock_path:
        mock_path.return_value = tmp_path
        # default root path is constructed; ensure no crash if absent
        result = svc.discover_bundles()
        assert isinstance(result, list)


def test_cleanup() -> None:
    ctx = Context()
    _setup(ctx)
    svc: OKFFilesystemProviderService = ctx.okf_filesystem
    svc._discovered = ["x"]
    svc._cleanup()
    assert svc._discovered == []
