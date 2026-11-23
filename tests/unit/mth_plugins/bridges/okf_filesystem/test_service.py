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
    discovered = svc.discover_bundles(roots=[(tmp_path, "project")])
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
    assert svc.discover_bundles(roots=[(tmp_path, "project")]) == []


async def test_discover_unreadable_index(tmp_path: Path) -> None:
    ctx = Context()
    _setup(ctx)
    await ctx.flush()
    svc = ctx.okf_filesystem
    _make_bundle(tmp_path, "bundle-x")
    with patch.object(Path, "read_text", side_effect=OSError("denied")):
        assert svc.discover_bundles(roots=[(tmp_path, "project")]) == []


async def test_discover_duplicate_loses_registration(tmp_path: Path) -> None:
    """When registry rejects a bundle it is not added to discovered list."""
    ctx = Context()
    _setup(ctx)
    await ctx.flush()
    svc = ctx.okf_filesystem
    _make_bundle(tmp_path, "bundle-dup")
    # First discovery registers it.
    assert svc.discover_bundles(roots=[(tmp_path, "project")]) == ["bundle-dup"]
    # Second discovery: registry tie => rejected => not in discovered.
    assert svc.discover_bundles(roots=[(tmp_path, "project")]) == []


async def test_discover_missing_root(tmp_path: Path) -> None:
    ctx = Context()
    _setup(ctx)
    await ctx.flush()
    svc = ctx.okf_filesystem
    assert svc.discover_bundles(roots=[(tmp_path / "missing", "project")]) == []


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


async def test_discover_bundles_with_scope_precedence(tmp_path: Path) -> None:
    ctx = Context()
    _setup(ctx)
    await ctx.flush()
    svc = ctx.okf_filesystem
    user = tmp_path / "user"
    home = tmp_path / "home"
    _make_bundle(user, "dup")
    _make_bundle(home, "dup")
    _make_bundle(home, "home-only")
    discovered = svc.discover_bundles(roots=[(home, "home"), (user, "user")])
    assert "dup" in discovered
    assert "home-only" in discovered
    # home (rank 1) beats user (rank 2)
    assert ctx.okf_registry.get_bundle("dup").path.parent == home


async def test_discover_bundles_symlink_dedup(tmp_path: Path) -> None:
    ctx = Context()
    _setup(ctx)
    await ctx.flush()
    svc = ctx.okf_filesystem
    real = tmp_path / "real"
    alias = tmp_path / "alias"
    real.mkdir()
    alias.mkdir()
    _make_bundle(real, "s")
    (alias / "s").symlink_to(real / "s", target_is_directory=True)
    discovered = svc.discover_bundles(roots=[(real, "home"), (alias, "user")])
    assert discovered.count("s") == 1


async def test_discover_bundles_no_roots(tmp_path: Path) -> None:
    ctx = Context()
    _setup(ctx)
    await ctx.flush()
    assert ctx.okf_filesystem.discover_bundles(roots=[]) == []
