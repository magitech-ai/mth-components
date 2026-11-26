"""Unit tests for the storage component entry point."""

from __future__ import annotations

from animas import Context

from mth_components.storage.component import apply
from mth_components.storage.service import StorageService


async def test_storage_component_apply() -> None:
    ctx = Context()
    await apply(ctx)
    assert ctx.has("storage")
    assert isinstance(ctx.storage, StorageService)
