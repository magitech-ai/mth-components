"""Anima component entry point for OKF provider registry."""

from __future__ import annotations

from typing import Any

from anima import Context

from mth_plugins.bridges.okf_registry.service import OKFProviderRegistryService


async def apply(
    ctx: Context,
    **kwargs: Any,
) -> None:
    """Mount OKF provider registry onto anima context.

    Registers OKFProviderRegistryService as ``ctx.okf_registry``.
    The filesystem provider discovers bundles and registers them here.
    """
    ctx.use(OKFProviderRegistryService)
    await ctx.flush()


apply.inject = ["okf_registry"]  # type: ignore[attr-defined]
