"""One opened JSON unit in ``per-record`` layout: a directory per unit.

Ports ``packages/storage/storage-json/src/per-record-unit.ts``. The directory
is the state: ``loadAll`` re-reads it, each write publishes one document, and
foreign or stale documents read as absent. Legacy whole-unit files bootstrap
a fresh tree when their version is accepted.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from mth_components.storage.service import StorageError
from mth_components.storage_json.atomic import (
    _publish,
    fsync_directory,
    mkdir_private,
    write_atomic,
)
from mth_components.storage_json.format import (
    MISSING,
    UnitState,
    parse_record,
    serialize_record,
)

SAFE_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _assert_safe_key(unit: str, key: str) -> None:
    if not SAFE_KEY_RE.fullmatch(key):
        raise ValueError(
            f"unit '{unit}': per-record key '{key}' is not path-safe "
            f"(must match {SAFE_KEY_RE.pattern})"
        )


def _accepted_stamps(descriptor: Any) -> tuple[int, ...]:
    return (descriptor["version"], *(descriptor.get("compatibleVersions") or ()))


async def _read_record(path: Path, versions: tuple[int, ...]) -> Any:
    try:
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return MISSING
    return parse_record(text, versions)


async def _load_table_records(
    records: dict[str, Any], versions: tuple[int, ...], directory: Path
) -> None:
    try:
        entries = list(await asyncio.to_thread(directory.iterdir))
    except FileNotFoundError:
        return
    for entry in entries:
        if not entry.name.endswith(".json"):
            continue
        key = entry.name[: -len(".json")]
        if not SAFE_KEY_RE.fullmatch(key):
            continue
        value = await _read_record(entry, versions)
        if value is not MISSING:
            records[key] = value


async def _bootstrap_legacy(descriptor: Any, directory: Path, state: UnitState) -> None:
    """Seed from ``<root>/<name>.json`` when no new-state directory exists."""
    legacy = directory.parent / f"{descriptor['name']}.json"
    try:
        text = await asyncio.to_thread(
            legacy.read_text, encoding="utf-8", errors="replace"
        )
    except FileNotFoundError:
        return
    # dsh's legacy bootstrap validates only the envelope and migrates the
    # well-formed tables; one bad table does not discard the good ones.
    try:
        envelope = json.loads(text)
    except (ValueError, TypeError):
        return
    if not isinstance(envelope, dict):
        return
    header = envelope.get("unit")
    if not isinstance(header, dict) or header.get("name") != descriptor["name"]:
        return
    stamped = header.get("version")
    if not isinstance(stamped, int) or isinstance(stamped, bool):
        return
    if stamped not in _accepted_stamps(descriptor):
        return
    tables = envelope.get("tables")
    if not isinstance(tables, dict):
        return
    for table, records in tables.items():
        target = state.tables.get(table)
        if target is None or not isinstance(records, dict):
            continue
        for key, value in records.items():
            path = directory / table / f"{key}.json"
            content = serialize_record(descriptor["version"], value)
            await asyncio.to_thread(mkdir_private, path.parent)
            await write_atomic(path, content)
            target[key] = value


async def _load_state(descriptor: Any, directory: Path) -> UnitState:
    versions = _accepted_stamps(descriptor)
    state = UnitState(
        version=descriptor["version"],
        tables={table: {} for table in descriptor["tables"]},
    )
    try:
        entries = list(await asyncio.to_thread(directory.iterdir))
    except FileNotFoundError:
        entries = []
    has_new_state = False
    for entry in entries:
        if entry.is_dir() and entry.name in state.tables:
            await _load_table_records(state.tables[entry.name], versions, entry)
            has_new_state = True
        elif entry.name == "global.json" and descriptor.get("hasGlobal"):
            global_value = await _read_record(entry, versions)
            if global_value is not MISSING:
                state.global_ = global_value
            has_new_state = True
    if not has_new_state:
        await _bootstrap_legacy(descriptor, directory, state)
    return state


def _backup_stamp() -> str:
    return datetime.now().strftime("%Y%m%d%H%M")


async def open_per_record_unit(
    descriptor: Any, root: Path, on_close: Callable[[], None]
) -> PerRecordJsonUnit:
    """Open a per-record unit; the opener touches nothing on the medium."""
    return PerRecordJsonUnit(descriptor, root / descriptor["name"], on_close)


class PerRecordJsonUnit:
    """Stateless unit: the directory is authoritative, writes publish one doc."""

    def __init__(
        self,
        descriptor: Any,
        directory: Path,
        on_close: Callable[[], None],
    ) -> None:
        self._descriptor = descriptor
        self._directory = directory
        self._on_close = on_close
        self._closed = False
        self._unusable: StorageError | None = None
        self._in_flight: set[asyncio.Task[Any]] = set()

    def _assert_open(self) -> None:
        if self._closed:
            raise StorageError("closed", f"unit '{self._descriptor['name']}' is closed")
        if self._unusable is not None:
            raise self._unusable

    def _table_dir(self, table: str) -> Path:
        if table not in self._descriptor["tables"]:
            raise ValueError(
                f"unit '{self._descriptor['name']}' does not declare table '{table}'"
            )
        return self._directory / table

    def _tracked(self, awaitable: Any) -> asyncio.Task[Any]:
        task = cast(asyncio.Task[Any], asyncio.ensure_future(awaitable))
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)
        return task

    async def _write_document(self, path: Path, value: Any) -> None:
        content = serialize_record(self._descriptor["version"], value)

        def write() -> None:
            mkdir_private(path.parent)
            write_atomic_sync(path, content)

        await asyncio.to_thread(write)

    async def loadAll(self) -> dict[str, Any]:  # noqa: N802
        """Re-read the directory and return all tables plus the global slot."""
        self._assert_open()
        state = await _load_state(self._descriptor, self._directory)
        return {
            "tables": {name: dict(records) for name, records in state.tables.items()},
            "global": state.global_,
        }

    async def putRecord(  # noqa: N802
        self, table: str, key: str, value: Any
    ) -> None:
        """Durably replace one record document."""
        self._assert_open()
        _assert_safe_key(self._descriptor["name"], key)
        await self._tracked(
            self._write_document(self._table_dir(table) / f"{key}.json", value)
        )

    async def deleteRecord(self, table: str, key: str) -> None:  # noqa: N802
        """Durably delete one record (idempotent)."""
        await self.deleteRecords(table, [key])

    async def deleteRecords(  # noqa: N802
        self, table: str, keys: tuple[str, ...] | list[str]
    ) -> None:
        """Stage document renames for the batch, then clean up staged copies."""
        self._assert_open()
        directory = self._table_dir(table)
        unique = list(dict.fromkeys(keys))
        for key in unique:
            _assert_safe_key(self._descriptor["name"], key)
        await self._tracked(self._remove_documents(directory, unique))

    async def _remove_documents(self, directory: Path, keys: list[str]) -> None:
        staged: list[tuple[Path, Path]] = []
        try:
            for key in keys:
                source = directory / f"{key}.json"
                destination = directory / f"{key}.json.deleting"
                try:
                    await asyncio.to_thread(source.rename, destination)
                except FileNotFoundError:
                    continue
                staged.append((source, destination))
            if staged:
                await asyncio.to_thread(fsync_directory, directory)
        except OSError as error:
            failures: list[OSError] = []
            for source, destination in staged:
                try:
                    await asyncio.to_thread(destination.rename, source)
                except OSError as restore_error:
                    failures.append(restore_error)
            if staged:
                try:
                    await asyncio.to_thread(fsync_directory, directory)
                except OSError as restore_error:
                    failures.append(restore_error)
            if failures:
                self._unusable = StorageError(
                    "unknown-state",
                    f"unit '{self._descriptor['name']}' could not restore a failed "
                    "batch delete; its state is unknown",
                )
                raise self._unusable from error
            raise
        for _, destination in staged:
            try:
                await asyncio.to_thread(destination.unlink)
            except OSError:
                pass

    async def backupRecord(self, table: str, key: str) -> str:  # noqa: N802
        """Move one document aside as ``<key>.json.bak.<stamp>``."""
        self._assert_open()
        _assert_safe_key(self._descriptor["name"], key)
        source = self._table_dir(table) / f"{key}.json"
        moved = source.with_name(f"{source.name}.bak.{_backup_stamp()}")
        await self._tracked(asyncio.to_thread(source.rename, moved))
        return str(moved)

    async def setGlobal(self, value: Any) -> None:  # noqa: N802
        """Durably replace the global singleton."""
        self._assert_open()
        if not self._descriptor.get("hasGlobal"):
            raise ValueError(
                f"unit '{self._descriptor['name']}' does not declare a global slot"
            )
        await self._tracked(
            self._write_document(self._directory / "global.json", value)
        )

    async def close(self) -> None:
        """Drain in-flight writes; idempotent."""
        was_closed = self._closed
        self._closed = True
        if self._in_flight:
            await asyncio.gather(*list(self._in_flight), return_exceptions=True)
        if was_closed:
            return
        self._on_close()


def write_atomic_sync(path: Path, data: str) -> None:
    """Synchronous atomic publisher used inside a worker thread."""
    _publish(path, data)
