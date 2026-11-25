"""Unit tests for OKF + Skills component entry points (apply functions)."""

from __future__ import annotations

from pathlib import Path

from animas import Context

from mth_components.bridges.okf_filesystem.component import apply as okf_fs_apply
from mth_components.bridges.okf_loader.component import apply as okf_loader_apply
from mth_components.bridges.okf_registry.component import apply as okf_registry_apply
from mth_components.bridges.skills_filesystem.component import apply as skills_fs_apply
from mth_components.bridges.skills_loader.component import apply as skills_loader_apply
from mth_components.bridges.skills_registry.component import (
    apply as skills_registry_apply,
)


async def test_okf_registry_apply() -> None:
    ctx = Context()
    await okf_registry_apply(ctx)
    await ctx.flush()
    assert ctx.has("okf_registry")


async def test_okf_filesystem_apply(tmp_path: Path) -> None:
    ctx = Context()
    await okf_fs_apply(ctx, knowledge_dir=tmp_path, auto_discover=True)
    await ctx.flush()
    assert ctx.has("okf_registry")
    assert ctx.has("okf_filesystem")


async def test_okf_filesystem_apply_no_auto() -> None:
    ctx = Context()
    await okf_fs_apply(ctx, auto_discover=False)
    await ctx.flush()
    assert ctx.has("okf_filesystem")


async def test_okf_loader_apply(tmp_path: Path) -> None:
    ctx = Context()
    await okf_loader_apply(ctx, auto_activate=True)
    await ctx.flush()
    assert ctx.has("okf_registry")
    assert ctx.has("okf_loader")


async def test_okf_loader_apply_no_auto() -> None:
    ctx = Context()
    await okf_loader_apply(ctx, auto_activate=False)
    await ctx.flush()
    assert ctx.has("okf_loader")


async def test_skills_registry_apply() -> None:
    ctx = Context()
    await skills_registry_apply(ctx)
    await ctx.flush()
    assert ctx.has("skills_registry")


async def test_skills_filesystem_apply(tmp_path: Path) -> None:
    ctx = Context()
    await skills_fs_apply(ctx, skills_dir=tmp_path, auto_discover=True)
    await ctx.flush()
    assert ctx.has("skills_registry")
    assert ctx.has("skills_filesystem")


async def test_skills_filesystem_apply_no_auto() -> None:
    ctx = Context()
    await skills_fs_apply(ctx, auto_discover=False)
    await ctx.flush()
    assert ctx.has("skills_filesystem")


async def test_skills_loader_apply(tmp_path: Path) -> None:
    ctx = Context()
    await skills_loader_apply(ctx, skills_dir=tmp_path, auto_activate=True)
    await ctx.flush()
    assert ctx.has("skills_registry")
    assert ctx.has("skills_loader")


async def test_skills_loader_apply_no_auto() -> None:
    ctx = Context()
    await skills_loader_apply(ctx, auto_activate=False)
    await ctx.flush()
    assert ctx.has("skills_loader")
