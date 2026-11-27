"""Animas component entry point for the local attachment backend."""

from __future__ import annotations

from typing import Any

from animas import Context

from mth_components.attachment_local.service import LocalAttachmentStore


async def apply(ctx: Context, **kwargs: Any) -> None:
    """Mount ``ctx.attachments`` over the configured harness home."""
    ctx.use(LocalAttachmentStore, dict(kwargs))
    await ctx.flush()
