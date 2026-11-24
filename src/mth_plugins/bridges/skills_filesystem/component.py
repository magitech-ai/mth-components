"""Animas component entry point for skills filesystem provider."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from animas import Context

from mth_plugins.bridges.skills_filesystem.service import (
    SkillsFilesystemProviderService,
)
from mth_plugins.bridges.skills_registry.service import SkillsProviderRegistryService


async def apply(
    ctx: Context,
    skills_dir: str | Path | None = None,
    scope: str = "project",
    auto_discover: bool = True,
    **kwargs: Any,
) -> None:
    """Mount skills filesystem provider onto animas context.

    Registers SkillsFilesystemProviderService and discovers skills under
    skills_dir with the given scope. For multi-root discovery with scope
    precedence, callers mount the service directly and pass ``roots`` to
    ``discover_skills`` (as home-swe-home's context.py does).
    """
    ctx.use(SkillsProviderRegistryService)
    ctx.use(SkillsFilesystemProviderService)
    await ctx.flush()

    if auto_discover and ctx.has("skills_registry"):
        service: SkillsFilesystemProviderService = ctx.skills_filesystem
        root = Path(skills_dir) if skills_dir else Path(".agents/skills")
        service.discover_skills(roots=[(root, scope)])


apply.inject = ["skills_filesystem"]  # type: ignore[attr-defined]
