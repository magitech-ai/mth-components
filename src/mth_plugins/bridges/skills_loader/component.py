"""Animas component entry point for skills session catalog + loader tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from animas import Context

from mth_plugins.bridges.skills_loader.service import SkillsSessionCatalogService
from mth_plugins.bridges.skills_registry.service import SkillsProviderRegistryService


async def apply(
    ctx: Context,
    skills_dir: str | Path | None = None,
    auto_activate: bool = True,
    **kwargs: Any,
) -> None:
    """Mount skills session catalog + loader tool onto animas context.

    Registers SkillsSessionCatalogService, injects catalog as
    <system-reminder> message, and provides skill({name}) tool.
    """
    ctx.use(SkillsProviderRegistryService)
    ctx.use(SkillsSessionCatalogService)
    await ctx.flush()

    if auto_activate and ctx.has("skills_registry"):
        service: SkillsSessionCatalogService = ctx.skills_loader
        service.activate()


apply.inject = ["skills_loader"]  # type: ignore[attr-defined]
