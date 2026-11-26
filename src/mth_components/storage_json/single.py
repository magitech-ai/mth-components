"""One opened JSON unit in ``single`` layout: one document per unit."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mth_components.storage.service import StorageError
from mth_components.storage_json.atomic import write_atomic
from mth_components.storage_json.format import UnitState, parse, serialize


async def open_single_unit(
    descriptor: Any, root: Path, on_close: Callable[[], None]
) -> SingleJsonUnit:
    """Open (load or lazily create) ``<root>/<name>.json``."""
    path = root / f"{descriptor['name']}.json"
    try:
        text = await asyncio.to_thread(
            path.read_text, encoding="utf-8", errors="replace"
        )
    except FileNotFoundError:
        state = UnitState(
            version=descriptor["version"],
            tables={table: {} for table in descriptor["tables"]},
        )
    else:
        state = parse(text, descriptor)
    # Opening never writes; the next publish uses the current stamp.
    state.version = descriptor["version"]
    return SingleJsonUnit(descriptor, path, state, on_close)


class SingleJsonUnit:
    """In-memory authoritative state republished atomically on every write."""

    def __init__(
        self,
        descriptor: Any,
        path: Path,
        state: UnitState,
        on_close: Callable[[], None],
    ) -> None:
        self._descriptor = descriptor
        self._path = path
        self._state = state
        self._on_close = on_close
        self._closed = False
        self._in_flight: set[asyncio.Task[None]] = set()

    def _assert_open(self) -> None:
        if self._closed:
            raise StorageError("closed", f"unit '{self._descriptor['name']}' is closed")

    def _records(self, table: str) -> dict[str, Any]:
        records = self._state.tables.get(table)
        if records is None:
            raise ValueError(
                f"unit '{self._descriptor['name']}' does not declare table '{table}'"
            )
        return records

    async def _publish(self) -> None:
        task = asyncio.create_task(
            write_atomic(self._path, serialize(self._descriptor["name"], self._state))
        )
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)
        await task

    async def loadAll(self) -> dict[str, Any]:  # noqa: N802
        """Return every table plus the global singleton."""
        self._assert_open()
        return {
            "tables": {
                name: dict(records) for name, records in self._state.tables.items()
            },
            "global": self._state.global_,
        }

    async def putRecord(  # noqa: N802
        self, table: str, key: str, value: Any
    ) -> None:
        """Durably upsert one record, rolling back memory on publish failure."""
        self._assert_open()
        records = self._records(table)
        had_key = key in records
        previous = records.get(key)
        records[key] = value
        try:
            await self._publish()
        except BaseException:
            if had_key:
                records[key] = previous
            else:
                del records[key]
            raise

    async def deleteRecord(self, table: str, key: str) -> None:  # noqa: N802
        """Durably delete one record (idempotent)."""
        await self.deleteRecords(table, [key])

    async def deleteRecords(  # noqa: N802
        self, table: str, keys: tuple[str, ...] | list[str]
    ) -> None:
        """Publish one whole-file replacement for the whole batch."""
        self._assert_open()
        records = self._records(table)
        removed: dict[str, Any] = {}
        for key in dict.fromkeys(keys):
            if key not in records:
                continue
            removed[key] = records[key]
            del records[key]
        if not removed:
            return
        try:
            await self._publish()
        except BaseException:
            records.update(removed)
            raise

    async def setGlobal(self, value: Any) -> None:  # noqa: N802
        """Durably write the global singleton, rolling back on failure."""
        self._assert_open()
        if not self._descriptor.get("hasGlobal"):
            raise ValueError(
                f"unit '{self._descriptor['name']}' does not declare a global slot"
            )
        previous = self._state.global_
        self._state.global_ = value
        try:
            await self._publish()
        except BaseException:
            self._state.global_ = previous
            raise

    async def close(self) -> None:
        """Drain in-flight writes; idempotent."""
        was_closed = self._closed
        self._closed = True
        await asyncio.gather(*list(self._in_flight), return_exceptions=True)
        if was_closed:
            return
        self._on_close()
