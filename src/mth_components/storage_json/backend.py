"""JSON storage backend: registers as ``json`` on the storage hub."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from mth_components.storage.service import (
    UNIT_NAME_RE,
    StorageError,
    storage_backend_service_key,
)
from mth_components.storage_json.atomic import mkdir_private
from mth_components.storage_json.per_record import open_per_record_unit
from mth_components.storage_json.single import open_single_unit


def validate_descriptor(descriptor: Any) -> None:
    """Validate unit and table names before opening a unit."""
    name = descriptor["name"]
    if not UNIT_NAME_RE.fullmatch(name):
        raise StorageError("malformed-medium", f"invalid unit name '{name}'")
    for table in descriptor["tables"]:
        if not UNIT_NAME_RE.fullmatch(table):
            raise StorageError(
                "malformed-medium", f"invalid table name '{table}' in unit '{name}'"
            )


class JsonStorageBackend:
    """One file-tree root serving both JSON layouts."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._open: dict[str, Any] = {}
        self._opening: dict[str, asyncio.Task[Any]] = {}
        self._closed = False

    @property
    def kv(self) -> Any:
        """The key-value facet."""
        return self

    async def open(self, descriptor: Any) -> Any:
        """Open one unit; a concurrent double-open is a caller bug."""
        if self._closed:
            raise StorageError("closed", "json backend is closed")
        validate_descriptor(descriptor)
        name = descriptor["name"]
        if name in self._open or name in self._opening:
            raise ValueError(
                f"unit '{name}' is already open; a unit has exactly one live handle"
            )
        task = asyncio.ensure_future(self._open_unit(descriptor))
        self._opening[name] = task
        try:
            return await task
        finally:
            self._opening.pop(name, None)

    async def _open_unit(self, descriptor: Any) -> Any:
        def prepare() -> None:
            mkdir_private(self._root)

        await asyncio.to_thread(prepare)
        name = descriptor["name"]

        def on_close() -> None:
            self._open.pop(name, None)

        def make_opener() -> Any:
            if descriptor.get("layout") == "per-record":
                return open_per_record_unit(descriptor, self._root, on_close)
            return open_single_unit(descriptor, self._root, on_close)

        unit = await make_opener()
        if self._closed:
            await unit.close()
            raise StorageError("closed", "json backend is closed")
        self._open[name] = unit
        return unit

    async def close(self) -> None:
        """Drain in-flight opens and close every live unit (idempotent)."""
        self._closed = True
        await asyncio.gather(*list(self._opening.values()), return_exceptions=True)
        for unit in list(self._open.values()):
            await unit.close()


def register_json_backend(ctx: Any, root: Path) -> JsonStorageBackend:
    """Register the backend on ``ctx.storage`` with effect-style cleanup."""

    registrations: list[Callable[[], None]] = []

    async def shutdown() -> None:
        for unregister in registrations:
            unregister()
        await backend.close()

    backend = JsonStorageBackend(root)

    def setup() -> Callable[[], Awaitable[None]]:
        registrations.append(ctx.storage.backend.register("json", backend))
        ctx.provide(storage_backend_service_key("json"), backend)
        return shutdown

    ctx.effect(setup)
    return backend
