"""Unit tests for the JSON storage backend and both unit layouts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from animas import Context

import mth_components.storage_json.per_record as pr
from mth_components.storage.service import StorageError, StorageService
from mth_components.storage_json import (
    MISSING,
    JsonStorageBackend,
    UnitState,
    parse,
    parse_record,
    serialize,
    serialize_record,
)
from mth_components.storage_json import atomic as atomic_module
from mth_components.storage_json import backend as backend_module
from mth_components.storage_json.atomic import mkdir_private
from mth_components.storage_json.backend import validate_descriptor
from mth_components.storage_json.component import apply
from mth_components.storage_json.per_record import (
    SAFE_KEY_RE,
    _load_table_records,
    open_per_record_unit,
)
from mth_components.storage_json.single import open_single_unit

DESCRIPTOR = {
    "name": "notes",
    "version": 2,
    "tables": ["items", "meta"],
    "hasGlobal": True,
}


def _descriptor(**overrides: Any) -> dict[str, Any]:
    return {**DESCRIPTOR, **overrides}


# -- format ----------------------------------------------------------------


def test_serialize_parse_roundtrip() -> None:
    state = UnitState(version=2, global_={"k": 1}, tables={"items": {"a": {"x": 1}}})
    text = serialize("notes", state)
    assert text.endswith("\n")
    parsed = parse(text, _descriptor())
    assert parsed.version == 2
    assert parsed.global_ == {"k": 1}
    assert parsed.tables["items"] == {"a": {"x": 1}}
    assert parsed.tables["meta"] == {}


def test_parse_errors_and_compatible_versions() -> None:
    with pytest.raises(StorageError) as bad_json:
        parse("not json", _descriptor())
    assert bad_json.value.code == "malformed-medium"
    with pytest.raises(StorageError, match="not a JSON object"):
        parse("[]", _descriptor())
    with pytest.raises(StorageError, match="foreign unit header"):
        parse(
            json.dumps({"unit": {"name": "other", "version": 2}, "tables": {}}),
            _descriptor(),
        )
    with pytest.raises(StorageError) as mismatch:
        parse(
            json.dumps({"unit": {"name": "notes", "version": 1}, "tables": {}}),
            _descriptor(),
        )
    assert mismatch.value.code == "version-mismatch"
    with pytest.raises(StorageError, match="tables is not an object"):
        parse(
            json.dumps({"unit": {"name": "notes", "version": 2}, "tables": []}),
            _descriptor(),
        )
    with pytest.raises(StorageError, match="is not an object"):
        parse(
            json.dumps(
                {"unit": {"name": "notes", "version": 2}, "tables": {"items": []}}
            ),
            _descriptor(),
        )
    accepted = parse(
        json.dumps({"unit": {"name": "notes", "version": 1}, "tables": {}}),
        _descriptor(compatibleVersions=[1]),
    )
    assert accepted.version == 1


def test_record_format_and_missing_sentinel() -> None:
    text = serialize_record(2, None)
    assert parse_record(text, (2,)) is None
    assert parse_record(text, (3,)) is MISSING
    assert parse_record("not json", (2,)) is MISSING
    assert parse_record("[]", (2,)) is MISSING
    assert parse_record(json.dumps({"version": "2", "record": 1}), (2,)) is MISSING
    assert parse_record(json.dumps({"version": 2, "record": "ok"}), (2,)) == "ok"
    assert parse_record(json.dumps({"version": 2}), (2,)) is MISSING
    assert json.loads(serialize_record(2, [float("nan"), {"x": float("inf")}]))[
        "record"
    ] == [None, {"x": None}]
    whole = serialize("notes", UnitState(version=2, global_=[float("nan")]))
    assert json.loads(whole)["global"] == [None]


# -- atomic ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_atomic(tmp_path: Path) -> None:
    target = tmp_path / "unit.json"
    await atomic_module.write_atomic(target, "one\n")
    assert target.read_text(encoding="utf-8") == "one\n"
    await atomic_module.write_atomic(target, "two\n")
    assert target.read_text(encoding="utf-8") == "two\n"


@pytest.mark.asyncio
async def test_fsync_directory_skips_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(atomic_module.sys, "platform", "win32")
    atomic_module.fsync_directory(tmp_path)


# -- single layout ---------------------------------------------------------


@pytest.mark.asyncio
async def test_single_unit_lifecycle(tmp_path: Path) -> None:
    unit = await open_single_unit(_descriptor(), tmp_path, lambda: None)
    assert (await unit.loadAll()) == {
        "tables": {"items": {}, "meta": {}},
        "global": None,
    }
    await unit.putRecord("items", "a", {"v": 1})
    await unit.putRecord("items", "b", 2)
    await unit.setGlobal({"g": True})
    assert (await unit.loadAll())["tables"]["items"] == {"a": {"v": 1}, "b": 2}
    await unit.deleteRecord("items", "a")
    await unit.deleteRecords("items", ["b", "absent"])
    await unit.deleteRecords("items", [])
    assert (await unit.loadAll())["tables"]["items"] == {}
    path = tmp_path / "notes.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["unit"] == {"name": "notes", "version": 2}

    reopened = await open_single_unit(_descriptor(), tmp_path, lambda: None)
    assert (await reopened.loadAll())["global"] == {"g": True}
    await reopened.close()
    await reopened.close()
    with pytest.raises(StorageError) as closed:
        await reopened.loadAll()
    assert closed.value.code == "closed"


@pytest.mark.asyncio
async def test_single_unit_guards_and_rollback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unit = await open_single_unit(_descriptor(hasGlobal=False), tmp_path, lambda: None)
    with pytest.raises(ValueError, match="does not declare table"):
        await unit.putRecord("missing", "k", 1)
    with pytest.raises(ValueError, match="does not declare a global slot"):
        await unit.setGlobal(1)

    async def fail_write(_path: Path, _data: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("mth_components.storage_json.single.write_atomic", fail_write)
    with pytest.raises(OSError):
        await unit.putRecord("items", "new", 1)
    assert (await unit.loadAll())["tables"]["items"] == {}

    (tmp_path / "notes.json").write_text(
        serialize("notes", UnitState(version=2, tables={"items": {"k": 1}})),
        encoding="utf-8",
    )
    stored = await open_single_unit(_descriptor(), tmp_path, lambda: None)
    with pytest.raises(OSError):
        await stored.deleteRecords("items", ["k"])
    assert (await stored.loadAll())["tables"]["items"] == {"k": 1}
    with pytest.raises(OSError):
        await stored.putRecord("items", "k", 2)
    assert (await stored.loadAll())["tables"]["items"] == {"k": 1}
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_single_unit_rejects_bad_medium(tmp_path: Path) -> None:
    (tmp_path / "notes.json").write_text("[]", encoding="utf-8")
    with pytest.raises(StorageError) as malformed:
        await open_single_unit(_descriptor(), tmp_path, lambda: None)
    assert malformed.value.code == "malformed-medium"


# -- per-record layout -----------------------------------------------------


@pytest.mark.asyncio
async def test_per_record_lifecycle(tmp_path: Path) -> None:
    unit = await open_per_record_unit(_descriptor(), tmp_path, lambda: None)
    assert SAFE_KEY_RE.fullmatch("ok_1-2")
    assert SAFE_KEY_RE.fullmatch("bad/key") is None
    await unit.putRecord("items", "a", {"v": 1})
    await unit.setGlobal("g")
    assert (await unit.loadAll())["tables"]["items"] == {"a": {"v": 1}}
    assert (await unit.loadAll())["global"] == "g"
    await unit.deleteRecords("items", ["a", "absent"])
    assert (await unit.loadAll())["tables"]["items"] == {}
    await unit.putRecord("items", "keep", 1)
    backup = await unit.backupRecord("items", "keep")
    assert backup.endswith(".bak.") or ".bak." in backup
    assert (await unit.loadAll())["tables"]["items"] == {}
    await unit.close()
    await unit.close()
    with pytest.raises(StorageError):
        await unit.loadAll()


@pytest.mark.asyncio
async def test_per_record_foreign_and_legacy(tmp_path: Path) -> None:
    unit = await open_per_record_unit(_descriptor(), tmp_path, lambda: None)
    directory = tmp_path / "notes" / "items"
    directory.mkdir(parents=True)
    (directory / "stale.json").write_text(serialize_record(1, "old"), encoding="utf-8")
    (directory / "bad.json").write_text("not json", encoding="utf-8")
    assert (await unit.loadAll())["tables"]["items"] == {}

    (tmp_path / "notes.json").write_text(
        serialize(
            "notes",
            UnitState(version=2, tables={"items": {"legacy": {"x": 1}}}),
        ),
        encoding="utf-8",
    )
    fresh = await open_per_record_unit(
        _descriptor(), tmp_path / "legacy-root", lambda: None
    )
    legacy_dir = tmp_path / "legacy-root" / "notes"
    legacy_dir.mkdir(parents=True)
    (tmp_path / "legacy-root" / "notes.json").write_text(
        (tmp_path / "notes.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert (await fresh.loadAll())["tables"]["items"] == {"legacy": {"x": 1}}
    await unit.close()
    await fresh.close()


@pytest.mark.asyncio
async def test_per_record_guards(tmp_path: Path) -> None:
    unit = await open_per_record_unit(
        _descriptor(hasGlobal=False), tmp_path, lambda: None
    )
    with pytest.raises(ValueError, match="not path-safe"):
        await unit.putRecord("items", "../bad", 1)
    with pytest.raises(ValueError, match="not path-safe"):
        await unit.deleteRecords("items", ["../bad"])
    with pytest.raises(ValueError, match="not path-safe"):
        await unit.backupRecord("items", "bad/key")
    with pytest.raises(ValueError, match="does not declare table"):
        await unit.putRecord("missing", "a", 1)
    with pytest.raises(ValueError, match="does not declare a global slot"):
        await unit.setGlobal(1)


@pytest.mark.asyncio
async def test_per_record_unknown_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unit = await open_per_record_unit(_descriptor(), tmp_path, lambda: None)
    await unit.putRecord("items", "a", 1)
    await unit.putRecord("items", "b", 2)
    original = Path.rename
    calls: list[tuple[str, str]] = []

    def staged_then_broken(self: Path, target: Path) -> None:
        calls.append((str(self), str(target)))
        if len(calls) == 2:
            raise OSError("stage failed")
        if ".deleting" in str(self) and str(target).endswith(".json"):
            raise OSError("restore failed")
        return original(self, target)

    monkeypatch.setattr(Path, "rename", staged_then_broken)
    with pytest.raises(StorageError) as unusable:
        await unit.deleteRecords("items", ["a", "b"])
    assert unusable.value.code == "unknown-state"
    with pytest.raises(StorageError) as again:
        await unit.loadAll()
    assert again.value.code == "unknown-state"
    await unit.close()


# -- backend and component -------------------------------------------------


def test_validate_descriptor() -> None:
    validate_descriptor(_descriptor())
    with pytest.raises(StorageError) as bad_unit:
        validate_descriptor(_descriptor(name="Bad"))
    assert bad_unit.value.code == "malformed-medium"
    with pytest.raises(StorageError, match="invalid table name"):
        validate_descriptor(_descriptor(tables=["Bad"]))


@pytest.mark.asyncio
async def test_backend_open_and_close(tmp_path: Path) -> None:
    backend = JsonStorageBackend(tmp_path)
    await backend.open(_descriptor())
    with pytest.raises(ValueError, match="already open"):
        await backend.open(_descriptor())
    await backend.close()
    await backend.close()
    with pytest.raises(StorageError) as closed:
        await backend.open(_descriptor())
    assert closed.value.code == "closed"


@pytest.mark.asyncio
async def test_component_apply(tmp_path: Path) -> None:
    ctx = Context()
    ctx.use(StorageService)
    await ctx.flush()
    await apply(ctx, root=str(tmp_path))
    backend = ctx.storage.backend.get("json")
    assert ctx.get("storage.backend.json") is backend
    assert ctx.storage.backend.names() == ["json"]
    await ctx.fiber.dispose()
    with pytest.raises(StorageError, match="closed"):
        await backend.open(_descriptor())
    with pytest.raises(ValueError, match="root"):
        await apply(Context(), root="")


# -- remaining branches ----------------------------------------------------


@pytest.mark.asyncio
async def test_atomic_cleanup_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_replace(self: Path, target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError):
        await atomic_module.write_atomic(tmp_path / "x.json", "data")
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.asyncio
async def test_backend_per_record_and_close_during_open(tmp_path: Path) -> None:
    backend = JsonStorageBackend(tmp_path)
    assert backend.kv is backend
    unit = await backend.open(_descriptor(layout="per-record"))
    await backend.close()
    with pytest.raises(StorageError, match="closed"):
        await unit.loadAll()

    blocked = asyncio.Event()

    async def slow_open(_descriptor: Any) -> Any:
        await blocked.wait()
        return object()

    async def release() -> None:
        await asyncio.sleep(0.01)
        blocked.set()

    pending = JsonStorageBackend(tmp_path)
    pending._open_unit = slow_open  # type: ignore[method-assign]
    task = asyncio.create_task(pending.open(_descriptor()))
    releaser = asyncio.create_task(release())
    await pending.close()
    await releaser
    with pytest.raises(StorageError, match="closed"):
        await task


@pytest.mark.asyncio
async def test_single_global_rollback_and_close_drain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unit = await open_single_unit(_descriptor(), tmp_path, lambda: None)
    await unit.putRecord("items", "seed", 1)

    async def fail_write(_path: Path, _data: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("mth_components.storage_json.single.write_atomic", fail_write)
    with pytest.raises(OSError):
        await unit.setGlobal("g")
    assert (await unit.loadAll())["global"] is None
    monkeypatch.undo()

    gate = asyncio.Event()

    async def slow_write(_path: Path, _data: str) -> None:
        await gate.wait()

    async def release() -> None:
        await asyncio.sleep(0.01)
        gate.set()

    monkeypatch.setattr("mth_components.storage_json.single.write_atomic", slow_write)
    task = asyncio.create_task(unit.putRecord("items", "late", 2))
    await asyncio.sleep(0)
    releaser = asyncio.create_task(release())
    await unit.close()
    await releaser
    await task
    await unit.close()


@pytest.mark.asyncio
async def test_per_record_read_and_legacy_branches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    await _load_table_records({}, (2,), tmp_path / "missing")
    unit = await open_per_record_unit(_descriptor(), tmp_path, lambda: None)
    directory = tmp_path / "notes" / "items"
    directory.mkdir(parents=True)
    (directory / "bad!key.json").write_text(serialize_record(2, 1), encoding="utf-8")
    (directory / "unreadable.json").mkdir()
    assert (await unit.loadAll())["tables"]["items"] == {}

    fresh_root = tmp_path / "fresh"
    fresh_root.mkdir()
    (fresh_root / "notes.json").write_text("not json", encoding="utf-8")
    fresh = await open_per_record_unit(_descriptor(), fresh_root, lambda: None)
    assert (await fresh.loadAll())["tables"]["items"] == {}

    (fresh_root / "notes.json").write_text(
        serialize("notes", UnitState(version=2, tables={"undeclared": {"x": 1}})),
        encoding="utf-8",
    )
    sharded = tmp_path / "sharded"
    sharded.mkdir()
    (sharded / "notes.json").write_text(
        serialize("notes", UnitState(version=2, tables={"undeclared": {"x": 1}})),
        encoding="utf-8",
    )
    second = await open_per_record_unit(_descriptor(), sharded, lambda: None)
    assert (await second.loadAll())["tables"]["items"] == {}
    await unit.close()
    await fresh.close()
    await second.close()


@pytest.mark.asyncio
async def test_per_record_delete_branches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unit = await open_per_record_unit(_descriptor(), tmp_path, lambda: None)
    await unit.putRecord("items", "a", 1)
    await unit.deleteRecord("items", "a")
    assert (await unit.loadAll())["tables"]["items"] == {}

    await unit.putRecord("items", "b", 2)
    original_fsync = pr.fsync_directory
    calls = 0

    def fail_first_fsync(_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("fsync failed")

    monkeypatch.setattr(pr, "fsync_directory", fail_first_fsync)
    with pytest.raises(OSError, match="fsync failed"):
        await unit.deleteRecords("items", ["b"])
    assert (await unit.loadAll())["tables"]["items"] == {"b": 2}
    monkeypatch.setattr(pr, "fsync_directory", original_fsync)

    await unit.putRecord("items", "c", 3)
    destination = tmp_path / "notes" / "items" / "c.json.deleting"
    original_unlink = Path.unlink

    def missing_unlink(self: Path) -> None:
        if self == destination:
            raise FileNotFoundError
        return original_unlink(self)

    monkeypatch.setattr(Path, "unlink", missing_unlink)
    await unit.deleteRecords("items", ["c"])
    assert (await unit.loadAll())["tables"]["items"] == {"b": 2}
    monkeypatch.undo()
    await unit.close()


@pytest.mark.asyncio
async def test_per_record_global_and_legacy_defensive_branches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    no_global = _descriptor(hasGlobal=False)
    directory = tmp_path / "notes"
    directory.mkdir()
    (directory / "global.json").write_text(
        serialize_record(2, "ignored"), encoding="utf-8"
    )
    unit = await open_per_record_unit(no_global, tmp_path, lambda: None)
    assert (await unit.loadAll())["global"] is None

    (directory / "global.json").write_text("not json", encoding="utf-8")
    with_global = await open_per_record_unit(_descriptor(), tmp_path, lambda: None)
    assert (await with_global.loadAll())["global"] is None

    legacy = tmp_path / "other" / "notes.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("not json", encoding="utf-8")
    empty = tmp_path / "other" / "notes"
    empty.mkdir()
    await pr._bootstrap_legacy(_descriptor(), empty, UnitState(version=2))
    fresh = tmp_path / "undeclared"
    fresh.mkdir()
    (fresh / "notes.json").write_text(
        json.dumps(
            {
                "unit": {"name": "notes", "version": 2},
                "tables": {"undeclared": {"x": 1}},
            }
        ),
        encoding="utf-8",
    )
    await pr._bootstrap_legacy(_descriptor(), fresh, UnitState(version=2))

    for payload in (
        "[]",
        "{}",
        json.dumps({"unit": {"name": "notes", "version": 2}}),
        json.dumps({"unit": {"name": "notes", "version": "2"}, "tables": {}}),
        json.dumps({"unit": {"name": "notes", "version": 9}, "tables": {}}),
        json.dumps({"unit": {"name": "notes", "version": 2}, "tables": []}),
    ):
        root = tmp_path / f"legacy-{len(payload)}-{payload[:8].replace('/', '_')}"
        root.mkdir(exist_ok=True)
        (root / "notes.json").write_text(payload, encoding="utf-8")
        await pr._bootstrap_legacy(_descriptor(), root / "notes", UnitState(version=2))
    await unit.close()
    await with_global.close()


@pytest.mark.asyncio
async def test_per_record_fsync_restore_branches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = await open_per_record_unit(_descriptor(), tmp_path / "one", lambda: None)
    await first.putRecord("items", "a", 1)
    failures = [0]

    def always_fail(_path: Path) -> None:
        failures[0] += 1
        raise OSError("fsync failed")

    monkeypatch.setattr(pr, "fsync_directory", always_fail)
    with pytest.raises(StorageError) as unknown:
        await first.deleteRecords("items", ["a"])
    assert unknown.value.code == "unknown-state"
    monkeypatch.undo()
    await first.close()

    second = await open_per_record_unit(_descriptor(), tmp_path / "two", lambda: None)
    await second.putRecord("items", "b", 2)
    calls = [0]

    def fail_then_pass(_path: Path) -> None:
        calls[0] += 1
        if calls[0] == 1:
            raise OSError("fsync failed")

    monkeypatch.setattr(pr, "fsync_directory", fail_then_pass)
    with pytest.raises(OSError, match="fsync failed"):
        await second.deleteRecords("items", ["b"])
    assert (await second.loadAll())["tables"]["items"] == {"b": 2}
    monkeypatch.undo()

    gate = asyncio.Event()

    async def slow_write(_path: Path, _data: str) -> None:
        await gate.wait()

    async def release() -> None:
        await asyncio.sleep(0.01)
        gate.set()

    monkeypatch.setattr(pr, "write_atomic", slow_write)
    task = asyncio.create_task(second.putRecord("items", "slow", 3))
    await asyncio.sleep(0)
    releaser = asyncio.create_task(release())
    await second.close()
    await releaser
    await task
    await second.close()


@pytest.mark.asyncio
async def test_backend_close_while_open_inflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _Unit:
        async def close(self) -> None:
            return None

    blocked = asyncio.Event()
    backend = JsonStorageBackend(tmp_path)

    async def slow_open(_descriptor: Any, _root: Path, _on_close: Any) -> Any:
        await blocked.wait()
        return _Unit()

    async def release() -> None:
        await asyncio.sleep(0.05)
        blocked.set()

    monkeypatch.setattr(backend_module, "open_single_unit", slow_open)
    task = asyncio.create_task(backend.open(_descriptor()))
    await asyncio.sleep(0)
    releaser = asyncio.create_task(release())
    await backend.close()
    await releaser
    with pytest.raises(StorageError, match="closed"):
        await task


@pytest.mark.asyncio
async def test_per_record_empty_batch_and_stage_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unit = await open_per_record_unit(_descriptor(), tmp_path, lambda: None)
    await unit.deleteRecords("items", ["absent"])
    original = Path.rename

    def fail_first(self: Path, target: Path) -> None:
        raise OSError("stage failed")

    monkeypatch.setattr(Path, "rename", fail_first)
    with pytest.raises(OSError, match="stage failed"):
        await unit.deleteRecords("items", ["absent2"])
    monkeypatch.undo()
    del original
    await unit.close()


@pytest.mark.asyncio
async def test_pinned_dsh_divergences_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Explicit null table is malformed, not an absent table.
    with pytest.raises(StorageError, match="is not an object"):
        parse(
            json.dumps(
                {"unit": {"name": "notes", "version": 2}, "tables": {"items": None}}
            ),
            _descriptor(),
        )

    # Invalid UTF-8 and foreign records read as absent.
    unit = await open_per_record_unit(_descriptor(), tmp_path / "utf8", lambda: None)
    directory = tmp_path / "utf8" / "notes" / "items"
    directory.mkdir(parents=True)
    (directory / "bad.json").write_bytes(b"\xff\xfe")
    assert (await unit.loadAll())["tables"]["items"] == {}
    await unit.close()

    # Legacy bootstrap migrates the good table even when another is malformed.
    legacy_root = tmp_path / "mixed"
    legacy_root.mkdir()
    (legacy_root / "notes.json").write_text(
        json.dumps(
            {
                "unit": {"name": "notes", "version": 2},
                "tables": {"items": {"good": 1}, "meta": []},
            }
        ),
        encoding="utf-8",
    )
    migrated = await open_per_record_unit(_descriptor(), legacy_root, lambda: None)
    assert (await migrated.loadAll())["tables"]["items"] == {"good": 1}
    await migrated.close()

    # Serialization fails before any directory is created (no bootstrap suppression).
    unserializable = tmp_path / "bad-value"
    failing = await open_per_record_unit(_descriptor(), unserializable, lambda: None)
    with pytest.raises((TypeError, ValueError)):
        await failing.putRecord("meta", "k", object())
    assert not (unserializable / "notes" / "meta").exists()
    await failing.close()


@pytest.mark.asyncio
async def test_pinned_close_and_modes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Cleanup OSError never rejects an already-staged deletion.
    deleted = await open_per_record_unit(
        _descriptor(), tmp_path / "cleanup", lambda: None
    )
    await deleted.putRecord("items", "a", 1)
    original_unlink = Path.unlink

    def deny_unlink(self: Path) -> None:
        if self.name.endswith(".deleting"):
            raise OSError("permission denied")
        return original_unlink(self)

    monkeypatch.setattr(Path, "unlink", deny_unlink)
    await deleted.deleteRecords("items", ["a"])
    assert (await deleted.loadAll())["tables"]["items"] == {}
    monkeypatch.undo()
    await deleted.close()

    # close() rejects operations started while it drains.
    draining = await open_per_record_unit(
        _descriptor(), tmp_path / "drain", lambda: None
    )
    gate = asyncio.Event()
    original_write = pr.write_atomic

    async def slow_write(path: Path, data: str) -> None:
        await gate.wait()
        return await original_write(path, data)

    monkeypatch.setattr(pr, "write_atomic", slow_write)
    task = asyncio.create_task(draining.putRecord("items", "late", 1))
    await asyncio.sleep(0)
    closing = asyncio.create_task(draining.close())
    await asyncio.sleep(0)
    with pytest.raises(StorageError, match="closed"):
        await draining.putRecord("items", "after-close", 2)
    gate.set()
    await closing
    await task

    # Published files are 0600 and directories 0700, independent of umask.
    modes = await open_per_record_unit(_descriptor(), tmp_path / "modes", lambda: None)
    await modes.putRecord("items", "a", 1)
    file_mode = (
        tmp_path / "modes" / "notes" / "items" / "a.json"
    ).stat().st_mode & 0o777
    unit_mode = (tmp_path / "modes" / "notes").stat().st_mode & 0o777
    dir_mode = (tmp_path / "modes" / "notes" / "items").stat().st_mode & 0o777
    assert file_mode == 0o600
    assert unit_mode == 0o700
    assert dir_mode == 0o700
    await modes.close()


@pytest.mark.asyncio
async def test_invalid_utf8_unit_and_private_root(tmp_path: Path) -> None:
    (tmp_path / "notes.json").write_bytes(b"\xff\xfe")
    with pytest.raises(StorageError) as malformed:
        await open_single_unit(_descriptor(), tmp_path, lambda: None)
    assert malformed.value.code == "malformed-medium"

    ctx = Context()
    ctx.use(StorageService)
    await ctx.flush()
    nested = tmp_path / "a" / "b" / "c"
    await apply(ctx, root=str(nested))
    await ctx.storage.backend.get("json").open(_descriptor())
    for parent in (tmp_path / "a", tmp_path / "a" / "b", nested):
        assert parent.stat().st_mode & 0o777 == 0o700


def test_mkdir_private_relative_and_concurrent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    mkdir_private(Path("one"))
    mkdir_private(Path("two/three"))
    assert (tmp_path / "one").is_dir()
    assert (tmp_path / "two" / "three").is_dir()
    assert (tmp_path / "one").stat().st_mode & 0o777 == 0o700

    async def concurrent() -> None:
        await asyncio.gather(
            *[asyncio.to_thread(mkdir_private, Path("race/nested")) for _ in range(8)]
        )

    asyncio.run(concurrent())
    assert (tmp_path / "race" / "nested").is_dir()
