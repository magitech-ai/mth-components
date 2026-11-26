"""Runtime of one open domain: in-memory state, one write chain, change events."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Iterator
from typing import Any, cast

from mth_components.storage.service import StorageError
from mth_components.storage_domain.error import DomainError

DOMAIN_CHANGED = "domain/changed"


class _TableHost:
    def __init__(self, domain: Any, table: str) -> None:
        self.domain = domain
        self.table = table

    def assert_readable(self) -> None:
        self.domain.assert_readable()

    def enqueue(self, job: Callable[[], Any]) -> Any:
        return self.domain.enqueue(job)

    def emit_put(self, key: str, value: Any) -> None:
        self.domain.emit_changed(
            {
                "domain": self.domain.name,
                "table": self.table,
                "key": key,
                "operation": "put",
                "value": value,
            }
        )

    def emit_deleted(self, key: str) -> None:
        self.domain.emit_changed(
            {
                "domain": self.domain.name,
                "table": self.table,
                "key": key,
                "operation": "deleted",
            }
        )


class KvTableImpl:
    """Table handle bound to one in-memory map and the domain write chain."""

    def __init__(self, domain: Any, table: str, records: dict[str, Any]) -> None:
        self._host = _TableHost(domain, table)
        self._table = table
        self._records = records

    def get(self, key: str) -> Any:
        self._host.assert_readable()
        return self._records.get(key)

    def entries(self) -> Iterator[tuple[str, Any]]:
        self._host.assert_readable()
        return iter(list(self._records.items()))

    def keys(self) -> Iterator[str]:
        self._host.assert_readable()
        return iter(list(self._records))

    @property
    def size(self) -> int:
        self._host.assert_readable()
        return len(self._records)

    async def put(self, key: str, value: Any) -> None:
        async def job() -> None:
            await self._host.domain.unit.putRecord(self._table, key, value)
            self._records[key] = value
            self._host.emit_put(key, value)

        await self._host.enqueue(job)

    async def delete(self, key: str) -> bool:
        async def job() -> bool:
            if key not in self._records:
                return False
            await self._host.domain.unit.deleteRecord(self._table, key)
            del self._records[key]
            self._host.emit_deleted(key)
            return True

        return cast(bool, await self._host.enqueue(job))

    async def delete_many(self, keys: Iterable[str]) -> int:
        async def job() -> int:
            present = list(dict.fromkeys(keys))
            present = [key for key in present if key in self._records]
            if not present:
                return 0
            await self._host.domain.unit.deleteRecords(self._table, present)
            for key in present:
                del self._records[key]
            for key in present:
                self._host.emit_deleted(key)
            return len(present)

        return cast(int, await self._host.enqueue(job))

    async def update(self, key: str, fn: Callable[[Any], Any]) -> Any:
        async def job() -> Any:
            if key not in self._records:
                raise DomainError(
                    "missing-key",
                    f"domain '{self._host.domain.name}' table '{self._table}' "
                    f"has no record '{key}' to update",
                )
            next_value = fn(self._records[key])
            await self._host.domain.unit.putRecord(self._table, key, next_value)
            self._records[key] = next_value
            self._host.emit_put(key, next_value)
            return next_value

        return await self._host.enqueue(job)


class DomainImpl:
    """The untyped runtime behind the spec-typed domain handle."""

    def __init__(  # noqa: PLR0917
        self,
        ctx: Any,
        spec: dict[str, Any],
        unit: Any,
        records: dict[str, dict[str, Any]],
        global_value: Any,
        on_closed: Callable[[], None],
    ) -> None:
        self.ctx = ctx
        self.name = spec["name"]
        self.unit = unit
        self.on_closed = on_closed
        self._tables = {
            table: KvTableImpl(self, table, values) for table, values in records.items()
        }
        self._global_value: Any = global_value
        self._global_handle: dict[str, Any] | None = None
        if spec.get("global") is not None:
            self._global_handle = {
                "get": self._global_get,
                "set": self._global_set,
            }
        self._chain: asyncio.Future[None] | None = None
        self._disposing = False
        self._failure: StorageError | None = None
        self._closed = False
        self._disposal: asyncio.Future[None] | None = None

    def _loop(self) -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    def _settled(self) -> asyncio.Future[None]:
        if self._chain is None:
            self._chain = self._loop().create_future()
            self._chain.set_result(None)
        return self._chain

    def table(self, table: str) -> KvTableImpl:
        handle = self._tables.get(table)
        if handle is None:
            raise ValueError(f"domain '{self.name}' declares no table '{table}'")
        return handle

    def _global_get(self) -> Any:
        self.assert_readable()
        return self._global_value

    async def _global_set(self, value: Any) -> None:
        async def job() -> None:
            await self.unit.setGlobal(value)
            self._global_value = value
            self.emit_changed(
                {
                    "domain": self.name,
                    "table": "",
                    "key": "",
                    "operation": "put",
                    "value": value,
                }
            )

        await self.enqueue(job)

    def assert_readable(self) -> None:
        if self._closed:
            raise DomainError("closed", f"domain '{self.name}' is closed")
        if self._failure is not None:
            raise self._failure

    def emit_changed(self, change: dict[str, Any]) -> None:
        try:
            self.ctx.emit(DOMAIN_CHANGED, change)
        except Exception as error:
            self.ctx.logger.warning(
                f"domain '{self.name}': domain/changed listener failed: {error}"
            )

    def enqueue(self, job: Callable[[], Any]) -> Any:
        if self._disposing:
            future = self._loop().create_future()
            future.set_exception(
                DomainError("closed", f"domain '{self.name}' is closed")
            )
            return future
        previous = self._settled()

        async def run() -> Any:
            if self._failure is not None:
                raise self._failure
            try:
                return await job()
            except StorageError as error:
                if error.code == "unknown-state":
                    self._failure = error
                raise

        async def chained() -> Any:
            await asyncio.shield(previous)
            return await run()

        task = asyncio.ensure_future(chained())
        self._chain = asyncio.ensure_future(_drain(task))
        return task

    def close(self) -> Any:
        if self._disposal is None:
            self._disposal = asyncio.ensure_future(self._run_close())
        return self._disposal

    async def _run_close(self) -> None:
        self._disposing = True
        if self._chain is not None:
            await asyncio.shield(self._chain)
        await self.unit.close()
        self._closed = True
        self.on_closed()


def _global_property(domain: DomainImpl) -> dict[str, Any]:
    if domain._global_handle is None:
        raise ValueError(f"domain '{domain.name}' declares no global")
    return domain._global_handle


setattr(DomainImpl, "global", property(_global_property))


async def _drain(task: asyncio.Future[Any]) -> None:
    try:
        await task
    except BaseException:
        pass


__all__ = ["DOMAIN_CHANGED", "DomainImpl", "KvTableImpl"]
