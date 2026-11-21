"""Unit tests for OKF provider registry service."""

from __future__ import annotations

from pathlib import Path

from anima import Context

from mth_plugins.bridges.okf_registry.service import (
    OKFBundleInfo,
    OKFProviderRegistryService,
)


def _bundle(name: str, rank: int = 0) -> OKFBundleInfo:
    return OKFBundleInfo(
        name=name,
        title=f"Title {name}",
        description=f"Desc {name}",
        path=Path("/tmp") / name,
        okf_version="0.3",
        rank=rank,
    )


def _registry(ctx: Context):
    ctx.use(OKFProviderRegistryService)
    return ctx.okf_registry


def test_register_and_get() -> None:
    ctx = Context()
    registry = _registry(ctx)
    bundle = _bundle("a")
    assert registry.register_bundle(bundle) is True
    assert registry.get_bundle("a") == bundle
    assert registry.get_bundle("missing") is None


def test_first_wins_by_rank() -> None:
    ctx = Context()
    registry = _registry(ctx)
    registry.register_bundle(_bundle("a", rank=5))
    # Lower rank wins.
    better = _bundle("a", rank=1)
    assert registry.register_bundle(better) is True
    assert registry.get_bundle("a") == better
    # Equal/higher rank loses.
    assert registry.register_bundle(_bundle("a", rank=1)) is False
    assert registry.register_bundle(_bundle("a", rank=9)) is False


def test_list_bundles_sorted() -> None:
    ctx = Context()
    registry = _registry(ctx)
    registry.register_bundle(_bundle("z"))
    registry.register_bundle(_bundle("a"))
    names = [b.name for b in registry.list_bundles()]
    assert names == ["a", "z"]


def test_cleanup() -> None:
    ctx = Context()
    registry = _registry(ctx)
    registry.register_bundle(_bundle("a"))
    assert len(registry.bundles) == 1
    registry._cleanup()
    assert len(registry.bundles) == 0
