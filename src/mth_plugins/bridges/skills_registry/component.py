"""Animas component entry point for skills provider registry."""

from __future__ import annotations

from typing import Any

from animas import Context

from mth_plugins.bridges.skills_registry.service import SkillsProviderRegistryService


async def apply(
    ctx: Context,
    **kwargs: Any,
) -> None:
    """Mount skills provider registry onto animas context.

    Registers SkillsProviderRegistryService as ``ctx.skills_registry``.
    The filesystem provider discovers skills and registers them here.
    """
    ctx.use(SkillsProviderRegistryService)
    await ctx.flush()


apply.inject = ["skills_registry"]  # type: ignore[attr-defined]
