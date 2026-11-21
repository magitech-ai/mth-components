"""Anima component entry point for OKF filesystem provider."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from anima import Context

from mth_plugins.bridges.okf_filesystem.service import OKFFilesystemProviderService
from mth_plugins.bridges.okf_registry.service import OKFProviderRegistryService


async def apply(
    ctx: Context,
    knowledge_dir: str | Path | None = None,
    auto_discover: bool = True,
    **kwargs: Any,
) -> None:
    """Mount OKF filesystem provider onto anima context.

    Registers OKFFilesystemProviderService and discovers OKF bundles
    under knowledge_dir (default: agents-home/knowledge).
    """
    ctx.use(OKFProviderRegistryService)
    ctx.use(OKFFilesystemProviderService)
    await ctx.flush()

    if auto_discover and ctx.has("okf_registry"):
        service: OKFFilesystemProviderService = ctx.okf_filesystem
        root = Path(knowledge_dir) if knowledge_dir else None
        service.discover_bundles(root=root)


apply.inject = ["okf_filesystem"]  # type: ignore[attr-defined]
