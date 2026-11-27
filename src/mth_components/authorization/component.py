"""Animas component entry point for the authorization seam."""

from __future__ import annotations

from typing import Any

from animas import Context

from mth_components.authorization.service import AuthorizationService


async def apply(ctx: Context, **kwargs: Any) -> None:
    """Mount ``ctx.authorization``."""
    del kwargs
    ctx.use(AuthorizationService)
    await ctx.flush()


apply.inject = ["credentials"]  # type: ignore[attr-defined]
