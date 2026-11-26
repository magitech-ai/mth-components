"""Animas component entry point for the JSON storage backend."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from animas import Context

from mth_components.storage_json.backend import register_json_backend


async def apply(ctx: Context, **kwargs: Any) -> None:
    """Mount the ``json`` backend on ``ctx.storage``.

    ``root`` is required: a cwd fallback would scatter unit files wherever the
    process happens to start.
    """
    root = kwargs.get("root")
    if not isinstance(root, str) or not root:
        raise ValueError("storage-json: 'root' is required")
    register_json_backend(ctx, Path(root))
    await ctx.flush()


apply.inject = ["storage"]  # type: ignore[attr-defined]
