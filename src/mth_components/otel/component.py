"""Animas component entry point for the shared OTel service."""

from __future__ import annotations

from typing import Any

from animas import Context

from mth_components.otel.service import OtelService


async def apply(ctx: Context, **kwargs: Any) -> None:
    """Mount ``ctx.otel``; no queue, identity, or transport is created."""
    del kwargs
    ctx.use(OtelService)
    await ctx.flush()
