"""Unit tests for the profile config editor."""

from __future__ import annotations

import copy
import inspect
from types import SimpleNamespace
from typing import Any

import pytest
from animas import Context, FiberState

import mth_components.config_editor.service as editor_module
from mth_components.config_editor import component as hmr_component
from mth_components.config_editor.component import apply
from mth_components.config_editor.service import ConfigEditorService, resolve_patch_path


def _entry(
    identifier: str,
    config: dict[str, Any] | None = None,
    *,
    name: str | None = None,
    fiber: Any = "default",
) -> Any:
    options: dict[str, Any] = {"id": identifier, "config": copy.deepcopy(config or {})}
    if name is not None:
        options["name"] = name
    if fiber == "default":
        fiber = SimpleNamespace(state=FiberState.ACTIVE)
    return SimpleNamespace(options=options, fiber=fiber)


class _Loader:
    def __init__(self, entries: list[Any]) -> None:
        self._entries = entries
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.fail_update = False

    def entries(self) -> list[Any]:
        return self._entries

    async def update(self, identifier: str, config: dict[str, Any]) -> None:
        if self.fail_update:
            raise RuntimeError("update failed")
        self.updated.append((identifier, copy.deepcopy(config)))
        for entry in self._entries:
            if (
                isinstance(entry.options, dict)
                and entry.options.get("id") == identifier
            ):
                entry.options.update(copy.deepcopy(config))


def _ctx(entries: list[Any]) -> tuple[Context, _Loader]:
    ctx = Context()
    loader = _Loader(entries)
    ctx.provide("loader", loader)
    return ctx, loader


def test_resolve_patch_path(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_patch_path({"path": str(tmp_path / "p.yml")}) == tmp_path / "p.yml"
    monkeypatch.setenv("MAGITECH_HOME", str(tmp_path))
    assert resolve_patch_path({}) == tmp_path / "profile.patch.yml"
    monkeypatch.delenv("MAGITECH_HOME")
    monkeypatch.setenv("MTH_HOME", str(tmp_path / "mth"))
    assert resolve_patch_path({}) == tmp_path / "mth" / "profile.patch.yml"
    monkeypatch.delenv("MTH_HOME")
    monkeypatch.setenv("MAGITECH_HOME", str(tmp_path / "mag"))
    assert resolve_patch_path({"dsh_home": str(tmp_path / "explicit")}) == (
        tmp_path / "explicit" / "profile.patch.yml"
    )


def test_entries_and_configuration(tmp_path: Any) -> None:
    first = _entry("a", {"k": 1, "own": 2}, name="A")
    second = _entry("b", {"z": 3})
    ctx, _loader = _ctx([first, second, SimpleNamespace(options=None)])
    patch = tmp_path / "patch.yml"
    patch.write_text(
        "- id: a\n  name: A\n  config:\n    own: 9\n- id: b\n  insert: before\n",
        encoding="utf-8",
    )
    layers = [{"id": "a", "config": {"k": 0, "own": 1}}]
    service = ConfigEditorService(ctx, {"path": str(patch), "layers": layers})
    assert service.document_path == patch
    assert service.entries() == [first, second]
    views = service.configuration()
    assert views[0]["inherited"] == {"k": 0, "own": 1}
    assert views[0]["override"] == {"own": 9}
    assert views[1]["override"] == {}

    empty = tmp_path / "empty.yml"
    empty.write_text("", encoding="utf-8")
    assert (
        ConfigEditorService(ctx, {"path": str(empty)}).configuration()[0]["inherited"]
        == {}
    )
    bad = tmp_path / "bad.yml"
    bad.write_text("not: a sequence\n", encoding="utf-8")
    with pytest.raises(ValueError, match="YAML sequence"):
        ConfigEditorService(ctx, {"path": str(bad)}).configuration()

    duplicate = _entry("dup")
    dup_ctx, _ = _ctx([duplicate, _entry("dup")])
    assert ConfigEditorService(dup_ctx, {"path": str(empty)}).entries() == []


@pytest.mark.asyncio
async def test_edit_persists_applies_and_rolls_back(tmp_path: Any) -> None:
    entry = _entry("a", {"k": 1}, name="A")
    ctx, loader = _ctx([entry])
    patch = tmp_path / "patch.yml"
    service = ConfigEditorService(ctx, {"path": str(patch)})

    await service.edit(entry, lambda current, inherited: {**current, "k": 2})
    assert loader.updated == [("a", {"config": {"k": 2}})]
    rows = editor_module._read_rows(patch)
    assert rows[0]["config"] == {"k": 2}
    assert patch.stat().st_mode & 0o777 == 0o600

    await service.edit(entry, lambda current, inherited: dict(inherited))
    assert "config" not in patch.read_text(encoding="utf-8")

    loader.fail_update = True
    with pytest.raises(RuntimeError, match="update failed"):
        await service.edit(entry, lambda current, inherited: {"k": 99})
    assert entry.options["config"] == {}
    assert not patch.exists() or "99" not in patch.read_text(encoding="utf-8")
    loader.fail_update = False

    with pytest.raises(ValueError, match="no longer available"):
        await service.edit(_entry("gone"), lambda current, inherited: {})
    with pytest.raises(TypeError, match="must be a mapping"):
        await service.edit(entry, lambda current, inherited: [])


@pytest.mark.asyncio
async def test_reset_removes_all_matching_rows_and_name_guard(tmp_path: Any) -> None:
    entry = _entry("a", {"k": 1}, name="A")
    ctx, _loader = _ctx([entry])
    patch = tmp_path / "patch.yml"
    patch.write_text(
        "- id: a\n  name: A\n  config:\n    k: 1\n"
        "- id: a\n  name: stale\n  config:\n    k: 1\n",
        encoding="utf-8",
    )
    service = ConfigEditorService(ctx, {"path": str(patch)})
    await service.edit(entry, lambda current, inherited: {})
    rows = editor_module._read_rows(patch)
    assert [row.get("name") for row in rows] == ["stale"]

    patch.write_text("- id: a\n  name: stale\n  config:\n    k: 9\n", encoding="utf-8")
    await service.edit(entry, lambda current, inherited: {**current, "k": 2})
    rows = editor_module._read_rows(patch)
    assert rows[0]["name"] == "stale"
    assert rows[-1]["name"] == "A"


@pytest.mark.asyncio
async def test_fiber_overlay_and_deep_copy_guards(tmp_path: Any) -> None:
    inactive = SimpleNamespace(state=FiberState.DISPOSED)
    entry = _entry("a", {"nested": {"x": 1}}, fiber=inactive)
    ctx, _loader = _ctx([entry])
    service = ConfigEditorService(ctx, {"path": str(tmp_path / "p.yml")})
    with pytest.raises(ValueError, match="no longer active"):
        await service.edit(entry, lambda current, inherited: {})

    active = _entry("a", {"nested": {"x": 1}})
    active_ctx, _ = _ctx([active])
    layered = ConfigEditorService(
        active_ctx,
        {
            "path": str(tmp_path / "p2.yml"),
            "layers": [{"id": "a", "config": {"nested": {"x": 0}}}],
        },
    )
    await layered.edit(
        active, lambda current, inherited: {**current, "nested": {"x": 5}}
    )

    overlaid = ConfigEditorService(
        active_ctx,
        {
            "path": str(tmp_path / "p2b.yml"),
            "overlays": [{"id": "a", "config": {"nested": {"x": 7}}}],
        },
    )
    with pytest.raises(ValueError, match="overridden"):
        await overlaid.edit(active, lambda current, inherited: {"nested": {"x": 5}})

    no_fiber = _entry("a", {"k": 1}, fiber=None)
    no_fiber_ctx, _ = _ctx([no_fiber])
    no_fiber_service = ConfigEditorService(
        no_fiber_ctx, {"path": str(tmp_path / "p2c.yml")}
    )
    with pytest.raises(ValueError, match="no longer active"):
        await no_fiber_service.edit(no_fiber, lambda current, inherited: {})

    def mutate(current: dict[str, Any], inherited: dict[str, Any]) -> dict[str, Any]:
        current["nested"]["x"] = 99
        return current

    plain = ConfigEditorService(active_ctx, {"path": str(tmp_path / "p3.yml")})
    await plain.edit(active, mutate)
    assert active.options["config"]["nested"]["x"] == 99


@pytest.mark.asyncio
async def test_hmr_serialization_fallback_and_component(tmp_path: Any) -> None:
    entry = _entry("a", {"k": 1})
    ctx, _loader = _ctx([entry])
    calls: list[str] = []

    class FakeHmr:
        async def run_exclusive(self, job: Any) -> Any:
            calls.append("enter")
            result = await job()
            calls.append("exit")
            return result

    ctx.provide("hmr", FakeHmr())
    service = ConfigEditorService(ctx, {"path": str(tmp_path / "patch.yml")})
    await service.edit(entry, lambda current, inherited: {**current, "k": 5})
    assert calls == ["enter", "exit"]

    legacy_ctx, legacy_loader = _ctx([_entry("a", {"k": 1})])
    legacy_ctx.provide("hmr", SimpleNamespace())
    legacy = ConfigEditorService(legacy_ctx, {"path": str(tmp_path / "legacy.yml")})
    await legacy.edit(
        legacy_ctx.loader.entries()[0], lambda current, inherited: {"k": 7}
    )
    assert legacy_loader.updated == [("a", {"config": {"k": 7}})]

    mounted = Context()
    mounted.provide("loader", _Loader([]))
    await apply(mounted, path=str(tmp_path / "other.yml"))
    assert isinstance(mounted.configEditor, ConfigEditorService)
    assert inspect.iscoroutinefunction(mounted.configEditor.edit)
    assert hmr_component.apply.inject == ["loader"]

    monkeypatch_failure = tmp_path / "failing.yml"
    fail_ctx, fail_loader = _ctx([_entry("a", {"k": 1})])
    fail_service = ConfigEditorService(fail_ctx, {"path": str(monkeypatch_failure)})
    original_open = editor_module.os.open

    def fail_open(*_args: Any, **_kwargs: Any) -> int:
        raise OSError("disk full")

    editor_module.os.open = fail_open  # type: ignore[assignment]
    try:
        with pytest.raises(OSError, match="disk full"):
            await fail_service.edit(
                fail_ctx.loader.entries()[0], lambda current, inherited: {"k": 3}
            )
    finally:
        editor_module.os.open = original_open  # type: ignore[assignment]
    del fail_loader


@pytest.mark.asyncio
async def test_remaining_editor_branches(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert editor_module._flatten({}) == []
    assert editor_module._flatten([1, {"id": "x", "config": [{"id": "y"}]}]) == [
        {"id": "x", "config": [{"id": "y"}]},
        {"id": "y"},
    ]

    monkeypatch.setattr(editor_module.sys, "platform", "win32")
    editor_module._fsync_directory(tmp_path)
    monkeypatch.undo()

    entry = _entry("a", {"k": 1}, name="A")
    ctx, loader = _ctx([entry])
    patch = tmp_path / "patch.yml"
    patch.write_text("- id: a\n  name: A\n  config:\n    k: 1\n", encoding="utf-8")
    layers = [
        {"id": "other", "config": {"k": 0}},
        {"id": "a", "config": "not-a-mapping"},
    ]
    service = ConfigEditorService(ctx, {"path": str(patch), "layers": layers})
    assert service.configuration()[0]["inherited"] == {}

    await service.edit(entry, lambda current, inherited: {"k": 2})
    assert editor_module._read_rows(patch)[0]["config"] == {"k": 2}

    loader.fail_update = True
    with pytest.raises(RuntimeError, match="update failed"):
        await service.edit(entry, lambda current, inherited: {"k": 3})
    assert editor_module._read_rows(patch)[0]["config"] == {"k": 2}
    loader.fail_update = False

    patch.write_text(
        "- id: a\n  config: not-a-mapping\n"
        "- id: a\n  name: A\n  extra: keep\n  config:\n    k: 1\n",
        encoding="utf-8",
    )
    await service.edit(entry, lambda current, inherited: {})
    assert editor_module._read_rows(patch)[0]["extra"] == "keep"
    assert service.configuration()[0]["override"] == {}


@pytest.mark.asyncio
async def test_rollback_without_existing_config_key(tmp_path: Any) -> None:
    entry = SimpleNamespace(
        options={"id": "a", "name": "A"},
        fiber=SimpleNamespace(state=FiberState.ACTIVE),
    )
    ctx, loader = _ctx([entry])
    loader.fail_update = True
    service = ConfigEditorService(ctx, {"path": str(tmp_path / "new.yml")})
    with pytest.raises(RuntimeError, match="update failed"):
        await service.edit(entry, lambda current, inherited: {"k": 1})
    assert "config" not in entry.options


@pytest.mark.asyncio
async def test_overlay_shape_branches(tmp_path: Any) -> None:
    entry = _entry("a", {"k": 1})
    ctx, _loader = _ctx([entry])
    non_list = ConfigEditorService(
        ctx, {"path": str(tmp_path / "p.yml"), "overlays": "not-a-list"}
    )
    await non_list.edit(entry, lambda current, inherited: {"k": 2})

    ignored = ConfigEditorService(
        ctx,
        {
            "path": str(tmp_path / "p2.yml"),
            "overlays": [{"id": "other", "config": {"k": 9}}, {"id": "a"}],
        },
    )
    await ignored.edit(entry, lambda current, inherited: {"k": 3})
