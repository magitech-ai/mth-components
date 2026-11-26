"""Unit tests for the domain data form."""

# ruff: noqa: N802, PLW0108

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from animas import Context

from mth_components.storage.service import (
    StorageError,
    StorageService,
    storage_backend_service_key,
)
from mth_components.storage_domain import (
    DOMAIN_CHANGED,
    DomainError,
    DomainFacility,
    Validator,
    define_domain,
    descriptor_of,
    domain_table,
)
from mth_components.storage_domain.component import apply
from mth_components.storage_json import JsonStorageBackend


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected number")
    return float(value)


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("expected string")
    return value


def _spec(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "prefs",
        "version": 1,
        "tables": {"items": domain_table(Validator(lambda v: _number(v)))},
    }
    base.update(overrides)
    return define_domain(base)


class _Backend:
    def __init__(self, unit: Any | None = None, *, kv: bool = True) -> None:
        self._unit = unit
        self.kv = self if kv else None
        self.opened: list[Any] = []
        self.closed = False

    async def open(self, descriptor: Any) -> Any:
        if self._unit is None:
            raise AssertionError("no unit configured")
        self.opened.append(descriptor)
        return self._unit

    async def close(self) -> None:
        self.closed = True


class _Unit:
    def __init__(
        self,
        tables: dict[str, dict[str, Any]] | None = None,
        global_value: Any = None,
    ) -> None:
        self.tables = tables or {"items": {}}
        self.global_value = global_value
        self.closed = False
        self.writes: list[tuple[str, Any]] = []
        self.events: list[Any] = []

    async def loadAll(self) -> dict[str, Any]:
        return {"tables": self.tables, "global": self.global_value}

    async def putRecord(self, table: str, key: str, value: Any) -> None:
        self.tables.setdefault(table, {})[key] = value
        self.writes.append(("put", (table, key, value)))

    async def deleteRecord(self, table: str, key: str) -> None:
        self.tables.get(table, {}).pop(key, None)
        self.writes.append(("delete", (table, key)))

    async def deleteRecords(self, table: str, keys: Any) -> None:
        for key in keys:
            self.tables.get(table, {}).pop(key, None)
        self.writes.append(("deleteMany", (table, list(keys))))

    async def setGlobal(self, value: Any) -> None:
        self.global_value = value
        self.writes.append(("global", value))

    async def backupRecord(self, table: str, key: str) -> str:
        self.tables.get(table, {}).pop(key, None)
        self.writes.append(("backup", (table, key)))
        return f"/backup/{key}"

    async def close(self) -> None:
        self.closed = True


async def _ctx(backend: Any) -> Context:
    ctx = Context()
    ctx.use(StorageService)
    await ctx.flush()
    ctx.storage.backend.register("json", backend)
    return ctx


# -- spec ------------------------------------------------------------------


def test_spec_validation_and_descriptor() -> None:
    spec = _spec(
        layout="per-record",
        compatibleVersions=[0],
        invalidRecords="backup-and-skip",
        **{"global": {"schema": Validator(lambda v: _string(v)), "initial": "init"}},
    )
    assert descriptor_of(spec) == {
        "name": "prefs",
        "version": 1,
        "tables": ["items"],
        "hasGlobal": True,
        "layout": "per-record",
        "compatibleVersions": [0],
    }
    assert descriptor_of(_spec())["hasGlobal"] is False
    assert "valueSchema" in domain_table(Validator(lambda v: _string(v)))

    bad_specs = (
        {"name": "Bad", "version": 1, "tables": {}},
        {"name": "ok", "version": -1, "tables": {}},
        {"name": "ok", "version": 1, "compatibleVersions": [1], "tables": {}},
        {"name": "ok", "version": 1, "compatibleVersions": [-1], "tables": {}},
        {"name": "ok", "version": 1, "layout": "weird", "tables": {}},
        {"name": "ok", "version": 1, "invalidRecords": "skip", "tables": {}},
        {"name": "ok", "version": 1, "tables": {"Bad": {}}},
        {"name": "ok", "version": 1, "tables": []},
        {
            "name": "ok",
            "version": 1,
            "tables": {},
            "global": {"schema": Validator(lambda v: v), "initial": None},
        },
    )
    for spec in bad_specs:
        with pytest.raises(ValueError):
            define_domain(spec)


def test_error_shape() -> None:
    error = DomainError(
        "invalid-record", "bad", detail={"table": "items", "key": "k"}, cause="why"
    )
    assert error.name == "DomainError"
    assert error.code == "invalid-record"
    assert error.detail == {"table": "items", "key": "k"}
    assert error.cause == "why"


# -- facility --------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_validates_loads_and_closes() -> None:
    unit = _Unit({"items": {"a": 1}}, global_value="stored")
    backend = _Backend(unit)
    ctx = await _ctx(backend)
    facility = DomainFacility(ctx, {"backend": "json"})
    spec = _spec(
        **{"global": {"schema": Validator(lambda v: _string(v)), "initial": "init"}}
    )

    domain = await facility.open(spec)
    assert facility.get("prefs") is domain
    assert domain.table("items").get("a") == 1
    assert getattr(domain, "global")["get"]() == "stored"
    assert backend.opened[0]["name"] == "prefs"

    with pytest.raises(DomainError, match="already open"):
        await facility.open(spec)
    await domain.close()
    assert unit.closed is True
    assert facility.get("prefs") is None
    reopened = await facility.open(spec)
    await facility.close_all()
    assert reopened.name == "prefs"


@pytest.mark.asyncio
async def test_open_refusals() -> None:
    ctx = await _ctx(_Backend(_Unit()))
    facility = DomainFacility(ctx, {"backend": "missing"})
    with pytest.raises(StorageError, match="not registered"):
        await facility.open(_spec())

    no_kv = _Backend(_Unit(), kv=False)
    no_kv_ctx = Context()
    no_kv_ctx.use(StorageService)
    await no_kv_ctx.flush()
    no_kv_ctx.storage.backend.register("json", no_kv)
    no_facet = DomainFacility(no_kv_ctx, {"backend": "json"})
    with pytest.raises(DomainError, match="has no kv facet"):
        await no_facet.open(_spec())

    bad_unit = _Unit({"items": {"a": "not a number"}})
    invalid = DomainFacility(await _ctx(_Backend(bad_unit)), {"backend": "json"})
    with pytest.raises(DomainError) as invalid_record:
        await invalid.open(_spec())
    assert invalid_record.value.code == "invalid-record"
    assert invalid_record.value.detail == {"table": "items", "key": "a"}
    assert bad_unit.closed is True

    backed_up = _Unit({"items": {"a": "bad"}})
    skipped = DomainFacility(await _ctx(_Backend(backed_up)), {"backend": "json"})
    domain = await skipped.open(_spec(invalidRecords="backup-and-skip"))
    assert domain.table("items").size == 0
    await domain.close()


@pytest.mark.asyncio
async def test_global_initial_and_validation() -> None:
    initial = _Unit({"items": {}}, global_value=None)
    ctx = await _ctx(_Backend(initial))
    facility = DomainFacility(ctx, {"backend": "json"})
    domain = await facility.open(
        _spec(
            **{"global": {"schema": Validator(lambda v: _string(v)), "initial": "init"}}
        )
    )
    assert getattr(domain, "global")["get"]() == "init"
    await domain.close()
    with pytest.raises(DomainError, match="closed"):
        getattr(domain, "global")["get"]()

    bad_global = _Unit({"items": {}}, global_value=42)
    bad_ctx = await _ctx(_Backend(bad_global))
    with pytest.raises(DomainError, match="global"):
        await DomainFacility(bad_ctx, {"backend": "json"}).open(
            _spec(
                **{
                    "global": {
                        "schema": Validator(lambda v: _string(v)),
                        "initial": "init",
                    }
                }
            )
        )

    plain = await DomainFacility(
        await _ctx(_Backend(_Unit())), {"backend": "json"}
    ).open(_spec())
    with pytest.raises(ValueError, match="declares no global"):
        _ = getattr(plain, "global")
    await plain.close()


# -- domain writes ---------------------------------------------------------


@pytest.mark.asyncio
async def test_domain_table_and_global_writes(tmp_path: Any) -> None:
    ctx = Context()
    ctx.use(StorageService)
    await ctx.flush()
    changes: list[Any] = []
    ctx.on(DOMAIN_CHANGED, changes.append)
    backend = JsonStorageBackend(tmp_path)
    ctx.storage.backend.register("json", backend)
    facility = DomainFacility(ctx, {"backend": "json"})
    spec = _spec(
        layout="per-record",
        **{"global": {"schema": Validator(lambda v: _string(v)), "initial": "init"}},
    )
    domain = await facility.open(spec)
    table = domain.table("items")
    assert table is domain.table("items")
    assert table.size == 0
    assert list(table.keys()) == []
    assert list(table.entries()) == []

    await table.put("a", 1)
    await table.put("b", 2)
    assert table.get("a") == 1
    assert dict(table.entries()) == {"a": 1, "b": 2}
    assert await table.update("a", lambda value: value + 10) == 11
    assert await table.delete("missing") is False
    assert await table.delete("b") is True
    assert await table.delete_many(["a", "unknown"]) == 1
    assert await table.delete_many([]) == 0
    with pytest.raises(DomainError, match="no record"):
        await table.update("gone", lambda value: value)

    await getattr(domain, "global")["set"]("next")
    assert getattr(domain, "global")["get"]() == "next"
    assert [change["operation"] for change in changes] == [
        "put",
        "put",
        "put",
        "deleted",
        "deleted",
        "put",
    ]
    assert changes[-1]["table"] == ""

    with pytest.raises(ValueError, match="declares no table"):
        domain.table("missing")
    await domain.close()


@pytest.mark.asyncio
async def test_domain_write_chain_close_and_quarantine(  # noqa: PLR0915
    tmp_path: Any,
) -> None:
    ctx = Context()
    ctx.use(StorageService)
    await ctx.flush()
    backend = JsonStorageBackend(tmp_path)
    ctx.storage.backend.register("json", backend)
    facility = DomainFacility(ctx, {"backend": "json"})
    domain = await facility.open(_spec(layout="per-record"))
    table = domain.table("items")

    first = table.put("a", 1)
    second = table.update("a", lambda value: value + 1)
    await asyncio.gather(first, second)
    assert table.get("a") == 2

    closing = domain.close()
    await asyncio.sleep(0)
    with pytest.raises(DomainError, match="closed"):
        await table.put("late", 3)
    with pytest.raises(DomainError, match="closed"):
        table.get("a")
    await closing
    await domain.close()
    assert facility.get("prefs") is None

    quarantined = await facility.open(_spec(name="other", layout="per-record"))
    poisoned = quarantined.table("items")
    unit = quarantined.unit

    async def poison(*_args: Any, **_kwargs: Any) -> None:
        raise StorageError("unknown-state", "unusable")

    unit.putRecord = poison  # type: ignore[method-assign]
    with pytest.raises(StorageError) as unknown:
        await poisoned.put("x", 1)
    assert unknown.value.code == "unknown-state"
    with pytest.raises(StorageError):
        poisoned.get("x")
    with pytest.raises(StorageError):
        await poisoned.put("y", 2)
    await quarantined.close()

    defiant = await facility.open(_spec(name="defiant", layout="per-record"))
    defiant_table = defiant.table("items")

    async def transient(*_args: Any, **_kwargs: Any) -> None:
        raise StorageError("closed", "transient")

    defiant.unit.putRecord = transient  # type: ignore[method-assign]
    with pytest.raises(StorageError, match="transient"):
        await defiant_table.put("z", 1)
    defiant_table.get("z")
    await defiant.close()

    listener_ctx = Context()
    listener_ctx.use(StorageService)
    await listener_ctx.flush()
    bad_backend = JsonStorageBackend(tmp_path / "listener")
    listener_ctx.storage.backend.register("json", bad_backend)
    listener_facility = DomainFacility(listener_ctx, {"backend": "json"})
    listener_domain = await listener_facility.open(
        _spec(name="listener", layout="per-record")
    )

    def bad_listener(_change: Any) -> None:
        raise RuntimeError("bad")

    listener_ctx.on(DOMAIN_CHANGED, bad_listener)
    await listener_domain.table("items").put("k", 1)
    await listener_domain.close()


@pytest.mark.asyncio
async def test_component_apply(tmp_path: Any) -> None:
    ctx = Context()
    ctx.use(StorageService)
    await ctx.flush()
    backend = JsonStorageBackend(tmp_path)
    ctx.storage.backend.register("json", backend)
    ctx.provide(storage_backend_service_key("json"), backend)
    await apply(
        ctx,
        backend="json",
        routes={"prefs": "json"},
    )
    assert ctx.get("storageDomain") is not None
    assert ctx.storage.form("domain") is ctx.storageDomain
    await ctx.fiber.dispose()
    with pytest.raises(ValueError, match="backend"):
        await apply(Context(), backend="")
