"""Unit tests for schema-derived settings forms and edits."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from animas import Context
from animas.schema import Schema

import mth_components.settings.service as module
from mth_components.settings import (
    SETTINGS_UPDATED,
    SettingsConflictError,
    SettingsService,
)
from mth_components.settings.component import apply


def _schema() -> Any:
    return Schema(
        "object",
        properties={
            "apiKey": Schema("string").role("secret").default("HARDCODED"),
            "mode": Schema("string").default("fast"),
            "nested": Schema(
                "object",
                properties={
                    "token": Schema("string").role("secret"),
                    "keep": Schema("number"),
                },
            ),
        },
    )


def _ctx(schemas: dict[str, Any] | None = None) -> Context:
    ctx = Context()
    ctx.provide(
        "settings",
        SettingsService(
            ctx,
            {
                "schemas": schemas or {"llm-demo": _schema()},
                "base": {"llm-demo": {"mode": "base", "nested": {"keep": 2}}},
            },
        ),
    )
    return ctx


def test_redaction_defaults_projection_and_describe() -> None:
    ctx = _ctx()
    service: SettingsService = ctx.settings
    service._user["llm-demo"] = {
        "apiKey": "secret",
        "nested": {"token": "t", "keep": 3},
    }
    view = service.describe("llm-demo")
    assert view["ns"] == "llm-demo"
    assert "apiKey" not in view["value"]
    assert view["value"]["nested"] == {"keep": 3}
    assert view["value"]["mode"] == "base"
    assert {tuple(item["path"]) for item in view["secrets"]} == {
        ("apiKey",),
        ("nested", "token"),
    }
    assert {item["set"] for item in view["secrets"]} == {True}
    assert "HARDCODED" not in str(view["schema"])
    assert view["base"]["mode"] == "base"
    assert view["revision"] == 0
    assert view["applies"] == "live"
    assert view["autoGenerate"] is True
    service.configure("llm-demo", auto=False)
    assert service.describe("llm-demo")["autoGenerate"] is False
    with pytest.raises(KeyError, match="not registered"):
        service.configure("missing", auto=True)
    assert service.namespaces() == ["llm-demo"]
    assert service.list()["hasDocument"] is False
    with pytest.raises(KeyError, match="not registered"):
        service.describe("missing")


def test_nested_dict_and_array_secret_paths() -> None:
    dict_schema = Schema(
        "object",
        properties={
            "creds": Schema(
                "dict",
                inner=Schema(
                    "object", properties={"token": Schema("string").role("secret")}
                ),
            )
        },
    )
    ctx = _ctx({"nested": dict_schema})
    service = ctx.settings
    service._user["nested"] = {"creds": {"gh": {"token": "SUPER-SECRET"}}}
    view = service.describe("nested")
    assert "SUPER-SECRET" not in str(view)
    assert any(
        item["path"] == ["creds", "gh", "token"] and item["set"]
        for item in view["secrets"]
    )

    array_schema = Schema(
        "object",
        properties={"items": Schema("array", inner=Schema("string").role("secret"))},
    )
    ctx2 = _ctx({"arr": array_schema})
    service2 = ctx2.settings
    service2._user["arr"] = {"items": ["one", "two"]}
    view2 = service2.describe("arr")
    assert view2["value"]["items"] == []
    assert [item["path"] for item in view2["secrets"]] == [
        ["items", "0"],
        ["items", "1"],
    ]


@pytest.mark.asyncio
async def test_replace_patch_conflict_and_events() -> None:
    ctx = _ctx()
    service = ctx.settings
    updated: list[tuple[str, int]] = []
    ctx.on(SETTINGS_UPDATED, lambda ns, revision: updated.append((ns, revision)))

    view = await service.replace("llm-demo", {"mode": "replaced", "apiKey": "s"}, 0)
    assert view["revision"] == 1
    assert view["value"]["mode"] == "replaced"
    assert updated == [("llm-demo", 1)]

    view = await service.patch(
        "llm-demo", [{"op": "set", "path": ["nested", "token"], "value": "t"}], 1
    )
    assert view["revision"] == 2
    assert view["value"]["nested"] == {"keep": 2}
    view = await service.patch("llm-demo", [{"op": "unset", "path": ["mode"]}], 2)
    assert view["value"]["mode"] == "base"
    view = await service.patch("llm-demo", [{"op": "set", "path": [], "value": {}}], 3)
    assert view["value"]["mode"] == "base"
    view = await service.patch("llm-demo", [{"op": "unset", "path": []}], 4)
    assert view["value"]["mode"] == "base"

    with pytest.raises(SettingsConflictError) as conflict:
        await service.replace("llm-demo", {}, 0)
    assert (conflict.value.expected, conflict.value.actual) == (0, 5)
    assert conflict.value.code == "SETTINGS_CONFLICT"
    with pytest.raises(KeyError, match="not registered"):
        await service.replace("missing", {}, 0)


@pytest.mark.asyncio
async def test_json_shape_array_ops_and_paths() -> None:
    ctx = _ctx()
    service = ctx.settings
    array_schema = Schema(
        "object", properties={"items": Schema("array", inner=Schema("string"))}
    )
    arrays = _ctx({"arr": array_schema}).settings
    await arrays.replace("arr", {"items": ["a", "b"]}, 0)
    view = await arrays.patch("arr", [{"op": "unset", "path": ["items", "0"]}], 1)
    assert view["value"]["items"] == ["b"]
    view = await arrays.patch(
        "arr", [{"op": "set", "path": ["items", "1"], "value": "c"}], 2
    )
    assert view["value"]["items"] == ["b", "c"]
    with pytest.raises(TypeError, match="out of range"):
        await arrays.patch("arr", [{"op": "unset", "path": ["items", "9"]}], 3)

    view = await service.replace("llm-demo", {"mode": None}, 0)
    assert view["value"]["mode"] is None
    with pytest.raises(TypeError, match="non-finite"):
        await service.replace("llm-demo", {"mode": float("inf")}, 1)
    with pytest.raises(TypeError, match="Config root"):
        await service.replace("llm-demo", [], 1)
    with pytest.raises(TypeError, match="Config root"):
        await service.patch("llm-demo", [{"op": "set", "path": [], "value": 1}], 1)
    with pytest.raises(TypeError, match="circular"):
        cyclic: dict[str, Any] = {}
        cyclic["self"] = cyclic
        await service.replace("llm-demo", cyclic, 1)
    with pytest.raises(TypeError, match="Config contains"):
        await service.replace("llm-demo", {"mode": object()}, 1)


@pytest.mark.asyncio
async def test_config_editor_preserves_non_form_config() -> None:
    ctx = _ctx()
    service = ctx.settings
    persisted: list[dict[str, Any]] = []

    class Editor:
        def entries(self) -> list[Any]:
            return [SimpleNamespace(options={"id": "llm-demo"})]

        async def edit(self, entry: Any, change: Any) -> None:
            raw = {"foreign": "kept", "mode": "stale"}
            result = change(raw, {})
            persisted.append(result)

    ctx.provide("configEditor", Editor())
    assert service.list()["hasDocument"] is True
    await service.replace("llm-demo", {"mode": "form"}, 0)
    assert persisted == [{"foreign": "kept", "mode": "form"}]


@pytest.mark.asyncio
async def test_listener_failures_are_contained() -> None:
    ctx = _ctx()
    service = ctx.settings

    def bad_listener(ns: Any, revision: Any) -> None:
        raise RuntimeError("listener failed")

    async def bad_async_listener(ns: Any, revision: Any) -> None:
        raise RuntimeError("async listener failed")

    ctx.on(SETTINGS_UPDATED, bad_listener)
    ctx.on(SETTINGS_UPDATED, bad_async_listener)
    assert (await service.replace("llm-demo", {"mode": "x"}, 0))["revision"] == 1
    await asyncio.sleep(0)


def test_helpers_branches() -> None:
    assert module._json_safe({"a": None}) == {"a": None}
    assert module._plain_schema({"no_refs": True}) == {"no_refs": True}
    envelope = {
        "uid": 1,
        "refs": {1: {"type": "object", "dict": {"a": 2}, "list": [3]}},
    }
    assert module._defaults(envelope, 1, {"a": 1}, {1}) == {"a": 1}
    assert module._project(envelope, 1, {"__jsExpr": "x"}) == {"__jsExpr": "x"}
    assert module._project(envelope, 1, {"a": 1, "extra": 2}) == {"a": 1}
    with pytest.raises(TypeError, match="does not address an object"):
        module._apply_op({"a": 1}, {"op": "set", "path": ["a", "b"], "value": 2})
    assert module._apply_op({}, {"op": "unset", "path": ["a"]}) == {}


@pytest.mark.asyncio
async def test_component_apply() -> None:
    ctx = Context()
    ctx.provide("configEditor", SimpleNamespace(entries=lambda: []))
    await apply(ctx, schemas={"demo": _schema()})
    assert isinstance(ctx.settings, SettingsService)
    assert ctx.settings.namespaces() == ["demo"]


def test_helper_branches_two() -> None:
    assert module._json_safe(3) == 3
    assert module._json_safe(1.5) == 1.5

    envelope = {
        "uid": 1,
        "refs": {
            1: {"type": "object", "dict": {"a": 2}, "list": [3], "inner": 4},
            2: {"type": "string", "meta": {"role": "secret"}},
            3: {"type": "string", "meta": {"role": "secret"}},
            4: {"type": "string"},
        },
    }
    assert module._redact(envelope, 1, {"a": "x"}, [], {1}) == ({"a": "x"}, [])
    assert module._redact(envelope, 1, {"a": "x"}, [])[0] == {}
    stripped = module._redact(envelope, 1, None, [], {1, 2, 3, 4})
    assert stripped == (None, [])
    assert module._plain_schema({"refs": {"bad": "not-a-mapping"}}) == {
        "refs": {"bad": "not-a-mapping"}
    }

    with pytest.raises(TypeError, match="out of range"):
        module._apply_op(
            {"items": ["a"]}, {"op": "set", "path": ["items", "9", "x"], "value": 1}
        )
    with pytest.raises(TypeError, match="does not address an object"):
        module._apply_op({"a": 1}, {"op": "set", "path": ["a", "b", "c"], "value": 1})
    assert module._apply_op(
        {"items": ["a"]}, {"op": "set", "path": ["items", "1"], "value": "b"}
    ) == {"items": ["a", "b"]}


@pytest.mark.asyncio
async def test_editor_without_entry_is_refused() -> None:
    ctx = _ctx()
    ctx.provide("configEditor", SimpleNamespace(entries=lambda: []))
    with pytest.raises(KeyError, match="no profile entry"):
        await ctx.settings.replace("llm-demo", {"mode": "x"}, 0)


def test_helper_branches_three() -> None:
    dict_secret = {
        "uid": 1,
        "refs": {
            1: {"type": "dict", "inner": 2},
            2: {"type": "string", "meta": {"role": "secret"}},
        },
    }
    assert module._redact(dict_secret, 1, {"gh": "secret"}, []) == (
        {},
        [{"path": ["gh"], "set": True}],
    )

    union = {
        "uid": 1,
        "refs": {
            1: {"type": "union", "list": [2]},
            2: {"type": "string", "meta": {"role": "secret"}},
        },
    }
    assert module._redact(union, 1, "secret", []) == (None, [{"path": [], "set": True}])
    transform = {
        "uid": 1,
        "refs": {
            1: {"type": "transform", "inner": 2},
            2: {"type": "string", "meta": {"role": "secret"}},
        },
    }
    assert module._redact(transform, 1, "secret", []) == (
        None,
        [{"path": [], "set": True}],
    )

    section = {"items": [{"mode": "a"}]}
    assert module._apply_op(
        section, {"op": "set", "path": ["items", "0", "mode"], "value": "b"}
    ) == {"items": [{"mode": "b"}]}
    with pytest.raises(TypeError, match="out of range"):
        module._apply_op(section, {"op": "set", "path": ["items", "9"], "value": "b"})
    assert module._apply_op(
        section, {"op": "set", "path": ["items", "0"], "value": "z"}
    ) == {"items": ["z"]}


def test_helper_branches_four() -> None:
    plain_union = {
        "uid": 1,
        "refs": {1: {"type": "union", "list": [2]}, 2: {"type": "string"}},
    }
    assert module._redact(plain_union, 1, "value", []) == ("value", [])
    plain_transform = {
        "uid": 1,
        "refs": {1: {"type": "transform", "inner": 2}, 2: {"type": "string"}},
    }
    assert module._redact(plain_transform, 1, "value", []) == ("value", [])


def test_helper_branches_five() -> None:
    envelope = {
        "refs": {
            0: {"type": "object", "dict": {"cfg": 1}},
            1: {
                "type": "object",
                "dict": {"token": 2},
                "meta": {"default": {"token": "NESTED-SECRET"}},
            },
            2: {"type": "string", "meta": {"role": "secret"}},
            3: {"type": "string", "meta": {"description": "plain"}},
            4: {"type": "union", "list": [2], "meta": {"default": "ALL-SECRET"}},
        }
    }
    plain = module._plain_schema(envelope)
    assert "NESTED-SECRET" not in str(plain)
    assert "ALL-SECRET" not in str(plain)

    secret_form = {
        "refs": {
            0: {"type": "object", "dict": {"token": 1}},
            1: {"type": "string", "meta": {"role": "secret"}},
        }
    }
    assert module._strip_form(secret_form, 0, {"token": None, "foreign": 1}) == {
        "foreign": 1
    }


@pytest.mark.asyncio
async def test_persistence_drops_form_fields_and_real_nested_defaults() -> None:
    ctx = _ctx()
    persisted: list[dict[str, Any]] = []

    class Editor:
        def entries(self) -> list[Any]:
            return [SimpleNamespace(options={"id": "llm-demo"})]

        async def edit(self, entry: Any, change: Any) -> None:
            raw = {
                "foreign": "keep",
                "mode": "stale",
                "nested": {"foreign_nested": "keep", "keep": 1},
            }
            persisted.append(change(raw, {}))

    ctx.provide("configEditor", Editor())
    await ctx.settings.patch("llm-demo", [{"op": "unset", "path": ["mode"]}], 0)
    assert "mode" not in persisted[-1]
    assert "foreign" in persisted[-1]
    await ctx.settings.replace("llm-demo", {}, 1)
    assert "mode" not in persisted[-1]
    assert persisted[-1]["nested"] == {"foreign_nested": "keep"}

    real_schema = Schema(
        "object",
        properties={
            "cfg": Schema(
                "object",
                properties={"token": Schema("string").role("secret")},
            ).default({"token": "NESTED-DEFAULT-SECRET"})
        },
    )
    ctx2 = _ctx({"real": real_schema})
    view = ctx2.settings.describe("real")
    assert "NESTED-DEFAULT-SECRET" not in str(view["schema"])
