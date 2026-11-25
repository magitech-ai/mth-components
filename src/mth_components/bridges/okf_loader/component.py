"""Animas component entry point for OKF session catalog + loader tool."""

from __future__ import annotations

from typing import Any

from animas import Context

from mth_components.bridges.okf_loader.service import OKFSessionCatalogService
from mth_components.bridges.okf_registry.service import OKFProviderRegistryService


async def apply(
    ctx: Context,
    auto_activate: bool = True,
    **kwargs: Any,
) -> None:
    """Mount OKF session catalog + loader tool onto animas context.

    Registers OKFSessionCatalogService, injects catalog as
    <system-reminder> message, and provides okf_bundle({name}) tool.
    """
    ctx.use(OKFProviderRegistryService)
    ctx.use(OKFSessionCatalogService)
    await ctx.flush()

    if auto_activate and ctx.has("okf_registry"):
        service: OKFSessionCatalogService = ctx.okf_loader
        service.activate()


apply.inject = ["okf_loader"]  # type: ignore[attr-defined]
