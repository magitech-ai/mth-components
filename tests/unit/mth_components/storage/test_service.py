"""Unit tests for the storage hub and its backend/form registries."""

from __future__ import annotations

from typing import Any

import pytest
from animas import Context

from mth_components.storage import (
    STORAGE_ERROR_CODES,
    UNIT_NAME_RE,
    BackendRegistry,
    StorageError,
    StorageService,
    storage_backend_service_key,
)


class _Backend:
    def __init__(self, name: str) -> None:
        self.name = name
        self.kv: Any = None
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _registry(ctx: Context) -> StorageService:
    ctx.use(StorageService)
    return ctx.storage


def test_error_taxonomy_and_unit_names() -> None:
    error = StorageError("closed", "unit is closed")
    assert error.code == "closed"
    assert error.name == "StorageError"
    assert str(error) == "unit is closed"
    assert {
        "backend-not-found",
        "form-not-mounted",
        "duplicate-backend",
        "duplicate-mount",
        "version-mismatch",
        "malformed-medium",
        "unknown-state",
        "closed",
    } <= STORAGE_ERROR_CODES
    assert UNIT_NAME_RE.fullmatch("notes_v2")
    assert UNIT_NAME_RE.fullmatch("a1_b") is not None
    assert UNIT_NAME_RE.fullmatch("1bad") is None
    assert UNIT_NAME_RE.fullmatch("Bad") is None
    assert UNIT_NAME_RE.fullmatch("has-dash") is None
    assert storage_backend_service_key("json") == "storage.backend.json"
    assert storage_backend_service_key("tenant-a") == "storage.backend.tenant-a"


def test_backend_registry_lifecycle() -> None:
    registry = BackendRegistry()
    backend = _Backend("json")
    dispose = registry.register("json", backend)
    assert registry.get("json") is backend
    assert registry.names() == ["json"]
    dispose()
    assert registry.names() == []
    assert backend.closed is False  # the hub unregisters; it never closes
    with pytest.raises(StorageError) as missing:
        registry.get("json")
    assert missing.value.code == "backend-not-found"
    assert "registered: none" in str(missing.value)


def test_backend_registry_duplicate_and_stale_disposer() -> None:
    registry = BackendRegistry()
    first = _Backend("first")
    second = _Backend("second")
    stale = registry.register("json", first)
    with pytest.raises(StorageError) as duplicate:
        registry.register("json", second)
    assert duplicate.value.code == "duplicate-backend"
    assert "already registered" in str(duplicate.value)

    stale()
    fresh = registry.register("json", second)
    stale()
    assert registry.get("json") is second
    fresh()
    assert registry.names() == []


def test_backend_registry_get_lists_registered_names() -> None:
    registry = BackendRegistry()
    registry.register("json", _Backend("json"))
    registry.register("sqlite", _Backend("sqlite"))
    with pytest.raises(StorageError) as missing:
        registry.get("missing")
    assert "registered: json, sqlite" in str(missing.value)


def test_storage_service_forms() -> None:
    ctx = Context()
    service = _registry(ctx)
    assert ctx.storage is service
    assert isinstance(service.backend, BackendRegistry)

    facility = {"marker": True}
    dispose = service.mount("domain", facility)
    assert service.form("domain") is facility
    assert service.domain is facility
    with pytest.raises(StorageError) as duplicate:
        service.mount("domain", {"other": True})
    assert duplicate.value.code == "duplicate-mount"
    assert "already mounted" in str(duplicate.value)

    dispose()
    with pytest.raises(StorageError) as missing:
        service.form("domain")
    assert missing.value.code == "form-not-mounted"
    with pytest.raises(StorageError):
        _ = service.domain


def test_storage_form_stale_disposer() -> None:
    ctx = Context()
    service = _registry(ctx)
    first = {"first": True}
    second = {"second": True}
    stale = service.mount("domain", first)
    stale()
    service.mount("domain", second)
    stale()
    assert service.domain is second
