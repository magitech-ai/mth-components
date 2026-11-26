"""Animas component entry point for the storage hub."""

from __future__ import annotations

from typing import Any

from animas import Context

from mth_components.storage.service import StorageService


async def apply(ctx: Context, **kwargs: Any) -> None:
    """Mount ``ctx.storage``."""
    del kwargs
    ctx.use(StorageService)
    await ctx.flush()
