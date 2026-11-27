"""Animas component entry point for profile plugin management."""

from __future__ import annotations

from typing import Any

from animas import Context

from mth_components.plugin_manager.service import PluginManager


async def apply(ctx: Context, **kwargs: Any) -> None:
    """Mount ``ctx.pluginManager`` over the configured profile root."""
    ctx.use(PluginManager, dict(kwargs))
    await ctx.flush()
