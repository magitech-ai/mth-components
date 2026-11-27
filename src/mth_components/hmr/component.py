"""Animas component entry point for the opt-in HMR wrapper."""

from __future__ import annotations

from typing import Any

from animas import Context
from animas.hmr import HmrService

from mth_components.hmr.config import resolve_hmr_config


async def apply(ctx: Context, **kwargs: Any) -> None:
    """Mount ``ctx.hmr`` over the animas HMR core."""
    ctx.use(HmrService, resolve_hmr_config(kwargs))
    await ctx.flush()


apply.inject = ["fs"]  # type: ignore[attr-defined]
