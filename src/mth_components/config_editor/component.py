"""Animas component entry point for the profile config editor."""

from __future__ import annotations

from typing import Any

from animas import Context

from mth_components.config_editor.service import ConfigEditorService


async def apply(ctx: Context, **kwargs: Any) -> None:
    """Mount ``ctx.configEditor``."""
    ctx.use(ConfigEditorService, kwargs)
    await ctx.flush()


apply.inject = ["loader"]  # type: ignore[attr-defined]
