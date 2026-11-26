"""Storage hub (``ctx.storage``): named backends and mounted data forms.

Ports ``packages/storage/storage/src/{index,registry,backend,error}.ts`` from
the pinned dsh source. The hub performs no IO: backends own media, data forms
own semantics. Multiple backends stay mounted side by side; consumers choose
which one to use.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, NotRequired, Protocol, TypedDict, runtime_checkable

from animas import Context, Service

#: Safe as a file name and as a SQL identifier segment without escaping.
UNIT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

STORAGE_ERROR_CODES = frozenset(
    {
        "backend-not-found",
        "form-not-mounted",
        "duplicate-backend",
        "duplicate-mount",
        "version-mismatch",
        "malformed-medium",
        "unknown-state",
        "closed",
    }
)


class StorageError(Exception):
    """Stable failure taxonomy for the storage hub and its backends."""

    name = "StorageError"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class KvUnitDescriptor(TypedDict):
    """Static identity and shape of one KV unit."""

    name: str
    version: int
    tables: list[str]
    hasGlobal: bool
    layout: NotRequired[str]
    compatibleVersions: NotRequired[list[int]]


@runtime_checkable
class KvUnit(Protocol):
    """One opened key-value unit; values are opaque JSON to this layer."""

    async def loadAll(self) -> dict[str, Any]:  # noqa: N802
        """Return all tables keyed by table name, plus the global singleton."""
        ...

    async def putRecord(  # noqa: N802
        self, table: str, key: str, value: Any
    ) -> None:
        """Durably upsert one record."""
        ...

    async def deleteRecord(self, table: str, key: str) -> None:  # noqa: N802
        """Durably delete one record (idempotent)."""
        ...

    async def deleteRecords(  # noqa: N802
        self, table: str, keys: Sequence[str]
    ) -> None:
        """Durably delete a batch of records."""
        ...

    async def setGlobal(self, value: Any) -> None:  # noqa: N802
        """Durably write the global singleton."""
        ...

    async def backupRecord(  # noqa: N802
        self, table: str, key: str
    ) -> str:
        """Move one record out of the readable set, preserving its bytes."""
        ...

    async def close(self) -> None:
        """Drain in-flight writes and release the unit (idempotent)."""
        ...


class KvFacet(Protocol):
    """Key-value capability of a storage backend, when it serves that form."""

    async def open(self, descriptor: KvUnitDescriptor) -> KvUnit:
        """Open one unit by static identity and shape."""
        ...


@runtime_checkable
class StorageBackend(Protocol):
    """One registered backend owning exactly one medium and its lifecycle."""

    @property
    def kv(self) -> KvFacet | None:
        """Key-value facet, or ``None`` when unsupported."""
        ...

    def close(self) -> Awaitable[None]:
        """Drain in-flight writes and release the medium (idempotent)."""
        ...


class BackendRegistry:
    """Mutable name → backend table with effect-style registration."""

    def __init__(self) -> None:
        self._backends: dict[str, StorageBackend] = {}

    def register(self, name: str, backend: StorageBackend) -> Callable[[], None]:
        """Register a named backend; the returned disposer removes that one."""
        if name in self._backends:
            raise StorageError(
                "duplicate-backend", f"storage backend '{name}' is already registered"
            )
        self._backends[name] = backend

        def dispose() -> None:
            if self._backends.get(name) is backend:
                del self._backends[name]

        return dispose

    def get(self, name: str) -> StorageBackend:
        """Resolve a backend by name; fail loud with the registered set."""
        backend = self._backends.get(name)
        if backend is None:
            registered = ", ".join(self._backends) or "none"
            raise StorageError(
                "backend-not-found",
                f"storage backend '{name}' is not registered "
                f"(registered: {registered})",
            )
        return backend

    def names(self) -> list[str]:
        """Return a snapshot of registered backend names."""
        return list(self._backends)


def storage_backend_service_key(name: str) -> str:
    """Derive the lifecycle service key contributed by a named backend."""
    return f"storage.backend.{name}"


class StorageService(Service):
    """The ``ctx.storage`` hub: registry plus mountable data forms."""

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx, "storage")
        self.backend = BackendRegistry()
        self._forms: dict[str, Any] = {}

    def mount(self, form: str, facility: Any) -> Callable[[], None]:
        """Mount a data-form facility; the disposer unmounts only this one."""
        if form in self._forms:
            raise StorageError(
                "duplicate-mount", f"storage form '{form}' is already mounted"
            )
        self._forms[form] = facility

        def dispose() -> None:
            if self._forms.get(form) is facility:
                del self._forms[form]

        return dispose

    def form(self, form: str) -> Any:
        """Resolve a mounted data form; fail loud when absent."""
        if form not in self._forms:
            raise StorageError(
                "form-not-mounted", f"storage form '{form}' is not mounted"
            )
        return self._forms[form]

    @property
    def domain(self) -> Any:
        """The domain data form, present once the domain plugin is loaded."""
        return self.form("domain")


__all__ = [
    "STORAGE_ERROR_CODES",
    "UNIT_NAME_RE",
    "BackendRegistry",
    "KvFacet",
    "KvUnit",
    "KvUnitDescriptor",
    "StorageBackend",
    "StorageError",
    "StorageService",
    "storage_backend_service_key",
]
