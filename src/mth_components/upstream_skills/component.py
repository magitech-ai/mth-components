"""Animas component entry point for upstream skills."""

from __future__ import annotations

from typing import Any

from animas import Context

from mth_components.upstream_skills.service import UpstreamSkillsService


async def apply(ctx: Context, **kwargs: Any) -> None:
    """Mount ``ctx.upstream_skills``."""
    ctx.use(UpstreamSkillsService, kwargs)
    await ctx.flush()


apply.inject = ["skills_registry"]  # type: ignore[attr-defined]
