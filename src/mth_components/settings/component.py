"""Animas component entry point for schema-derived settings forms."""

from __future__ import annotations

from typing import Any

from animas import Context

from mth_components.settings.service import SettingsService


async def apply(ctx: Context, **kwargs: Any) -> None:
    """Mount ``ctx.settings``."""
    ctx.use(SettingsService, dict(kwargs))
    await ctx.flush()


apply.inject = ["configEditor"]  # type: ignore[attr-defined]
