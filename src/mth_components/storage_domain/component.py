"""Animas component entry point for the domain data form."""

from __future__ import annotations

from typing import Any

from animas import Context

from mth_components.storage_domain.facility import apply as mount_domain


async def apply(ctx: Context, **kwargs: Any) -> None:
    """Mount ``ctx.storageDomain``."""
    backend = kwargs.get("backend")
    if not isinstance(backend, str) or not backend:
        raise ValueError("storage-domain: 'backend' is required")
    await mount_domain(ctx, kwargs)
    await ctx.flush()
