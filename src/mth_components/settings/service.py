"""Schema-derived settings forms and revision-checked edits over profile patches.

Ports the core of ``packages/settings/settings/src``: structural secret
redaction with concrete paths, schema defaults and form projection, path
addressed edits (including arrays), stale-write refusal, and persistence
through the mounted config editor. The wire Remote and protocol-specific pages
are not shipped here.
"""

from __future__ import annotations

import asyncio
import builtins
import copy
import inspect
import math
from collections.abc import Callable, Mapping
from typing import Any, cast

from animas import Context, Service

SETTINGS_UPDATED = "settings/document-updated"


class SettingsConflictError(Exception):
    """A write was based on a stale revision of a namespace's config."""

    name = "SettingsConflictError"
    code = "SETTINGS_CONFLICT"

    def __init__(self, ns: str, expected: int, actual: int) -> None:
        super().__init__(
            f'settings namespace "{ns}" changed since it was read '
            f"(expected revision {expected}, now {actual})"
        )
        self.ns = ns
        self.expected = expected
        self.actual = actual


def _json_safe(value: Any, seen: set[int] | None = None) -> Any:
    """Detach only lossless JSON data; ``None`` is a value, not absence."""
    seen = seen or set()
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("Config contains a non-finite number")
        return value
    if isinstance(value, (list, dict)):
        if id(value) in seen:
            raise TypeError("Config contains a circular reference")
        seen.add(id(value))
        result: Any
        if isinstance(value, list):
            result = [_json_safe(item, seen) for item in value]
        else:
            result = {key: _json_safe(item, seen) for key, item in value.items()}
        seen.discard(id(value))
        return result
    raise TypeError(f"Config contains a {type(value).__name__}")


def _node(schema: Mapping[str, Any], uid: Any) -> Mapping[str, Any]:
    refs = schema.get("refs", {})
    return refs.get(uid, {}) if isinstance(refs, dict) else {}


def _redact(  # noqa: PLR0912
    schema: Mapping[str, Any],
    uid: Any,
    value: Any,
    path: list[str],
    seen: set[Any] | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """Strip secret fields at their concrete paths, recording each slot."""
    seen = seen or set()
    if uid in seen:
        return copy.deepcopy(value), []
    seen.add(uid)
    definition = _node(schema, uid)
    if definition.get("meta", {}).get("role") == "secret":
        return None, [{"path": list(path), "set": value is not None}]
    secrets: list[dict[str, Any]] = []
    kind = definition.get("type")
    if kind == "object" and isinstance(value, dict):
        rebuilt = {
            key: copy.deepcopy(item)
            for key, item in value.items()
            if key not in (definition.get("dict") or {})
        }
        for key, child in (definition.get("dict") or {}).items():
            child_path = [*path, key]
            stripped, found = _redact(
                schema, child, value.get(key), child_path, set(seen)
            )
            if key in value and not any(entry["path"] == child_path for entry in found):
                rebuilt[key] = stripped
            secrets.extend(found)
        return rebuilt, secrets
    if kind == "dict" and isinstance(value, dict):
        rebuilt = {}
        for key, item in value.items():
            child_path = [*path, key]
            stripped, found = _redact(
                schema, definition.get("inner"), item, child_path, set(seen)
            )
            if not any(entry["path"] == child_path for entry in found):
                rebuilt[key] = stripped if stripped is not None else item
            secrets.extend(found)
        return rebuilt, secrets
    if kind in ("array", "list") and isinstance(value, list):
        rebuilt_items: list[Any] = []
        for index, item in enumerate(value):
            child_path = [*path, str(index)]
            stripped, found = _redact(
                schema, definition.get("inner"), item, child_path, set(seen)
            )
            if not any(entry["path"] == child_path for entry in found):
                rebuilt_items.append(stripped if stripped is not None else item)
            secrets.extend(found)
        return rebuilt_items, secrets
    for child in definition.get("list") or ():
        stripped, found = _redact(schema, child, value, path, set(seen))
        value = None if any(entry["path"] == path for entry in found) else stripped
        secrets.extend(found)
    if "inner" in definition:
        stripped, found = _redact(schema, definition["inner"], value, path, set(seen))
        value = None if any(entry["path"] == path for entry in found) else stripped
        secrets.extend(found)
    unique: dict[str, dict[str, Any]] = {}
    for entry in secrets:
        key = repr(entry["path"])
        previous = unique.get(key)
        unique[key] = {
            "path": entry["path"],
            "set": entry["set"] or (previous is not None and previous["set"]),
        }
    return value, list(unique.values())


def _plain_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Drop secret defaults/required before the schema crosses the wire."""
    result = copy.deepcopy(dict(schema))
    refs = result.get("refs")
    if not isinstance(refs, dict):
        return result
    for uid, definition in refs.items():
        if not isinstance(definition, dict):
            continue
        meta = definition.get("meta")
        if not isinstance(meta, dict):
            continue
        if meta.get("role") == "secret":
            meta.pop("default", None)
            meta.pop("required", None)
        elif "default" in meta:
            stripped, found = _redact(result, uid, meta["default"], [])
            if found:
                if stripped is None:
                    meta.pop("default", None)
                else:
                    meta["default"] = stripped
    return result


def _strip_form(
    schema: Mapping[str, Any], uid: Any, value: Any, seen: set[Any] | None = None
) -> Any:
    """Keep every non-form field at every depth of a raw config."""
    seen = seen or set()
    if uid in seen or not isinstance(value, dict):
        return copy.deepcopy(value)
    seen.add(uid)
    declared = _node(schema, uid).get("dict") or {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in declared:
            child = declared[key]
            stripped = _strip_form(schema, child, item, set(seen))
            kind = _node(schema, child).get("type")
            if isinstance(item, dict) and kind in ("object", "dict") and stripped:
                result[key] = stripped
        else:
            result[key] = copy.deepcopy(item)
    return result


def _defaults(
    schema: Mapping[str, Any], uid: Any, value: Any, seen: set[Any] | None = None
) -> Any:
    """Fill missing schema-declared defaults without overriding stored values."""
    seen = seen or set()
    if uid in seen:
        return value
    seen.add(uid)
    definition = _node(schema, uid)
    if definition.get("type") == "object" and isinstance(value, dict):
        result = dict(value)
        for key, child in (definition.get("dict") or {}).items():
            child_definition = _node(schema, child)
            if key not in result:
                default = child_definition.get("meta", {}).get("default")
                if default is not None:
                    result[key] = copy.deepcopy(default)
                continue
            result[key] = _defaults(schema, child, result[key], set(seen))
        return result
    return value


def _merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        result = dict(base)
        for key, value in override.items():
            result[key] = _merge(result.get(key), value)
        return result
    return copy.deepcopy(override)


def _project(schema: Mapping[str, Any], uid: Any, value: Any) -> Any:
    """Keep only schema-declared fields of a plain form value."""
    if isinstance(value, dict) and len(value) == 1 and "__jsExpr" in value:
        return value
    definition = _node(schema, uid)
    if definition.get("type") != "object" or not isinstance(value, dict):
        return value
    declared = definition.get("dict") or {}
    return {
        key: _project(schema, child, item)
        for key, item in value.items()
        if key in declared
        for child in [declared[key]]
    }


def _apply_op(  # noqa: PLR0912
    section: dict[str, Any], op: Mapping[str, Any]
) -> dict[str, Any]:
    result = copy.deepcopy(section)
    path = list(op.get("path") or [])
    target: Any = result
    for key in path[:-1]:
        if isinstance(target, list):
            if not str(key).isdigit() or int(key) >= len(target):
                raise TypeError(f'Config array index "{key}" is out of range')
            target = target[int(key)]
        else:
            if not isinstance(target, dict):
                raise TypeError("Config path does not address an object")
            if key not in target:
                target[key] = {}
            target = target[key]
    if not path:
        if op.get("op") == "unset":
            return {}
        if not isinstance(op.get("value"), dict):
            raise TypeError("Config root must be a plain object")
        return copy.deepcopy(op["value"])
    key = path[-1]
    if op.get("op") == "unset":
        if isinstance(target, list):
            if not str(key).isdigit() or int(key) >= len(target):
                raise TypeError(f'Config array index "{key}" is out of range')
            target.pop(int(key))
        else:
            target.pop(key, None)
    else:
        if not isinstance(target, (dict, list)):
            raise TypeError("Config path does not address an object")
        if isinstance(target, list):
            if not str(key).isdigit() or int(key) > len(target):
                raise TypeError(f'Config array index "{key}" is out of range')
            index = int(key)
            if index == len(target):
                target.append(copy.deepcopy(op.get("value")))
            else:
                target[index] = copy.deepcopy(op.get("value"))
        else:
            target[key] = copy.deepcopy(op.get("value"))
    return result


class SettingsService(Service):
    """Registry of namespace forms with revision-checked writes."""

    def __init__(self, ctx: Context, config: Mapping[str, Any] | None = None) -> None:
        super().__init__(ctx, "settings")
        cfg = dict(config or {})
        self._schemas: dict[str, Any] = dict(cfg.get("schemas") or {})
        self._base: dict[str, Any] = copy.deepcopy(dict(cfg.get("base") or {}))
        self._user: dict[str, Any] = copy.deepcopy(dict(cfg.get("user") or {}))
        self._revisions: dict[str, int] = dict.fromkeys(self._schemas, 0)
        self._auto = {
            key: bool((cfg.get("auto") or {}).get(key, True)) for key in self._schemas
        }
        self._lock = asyncio.Lock()

    def configure(self, ns: str, *, auto: bool) -> None:
        """Set whether a surface may auto-generate a page for *ns*."""
        if ns not in self._schemas:
            raise KeyError(f'settings namespace "{ns}" is not registered')
        self._auto[ns] = bool(auto)

    def namespaces(self) -> list[str]:
        """Registered namespace ids in declaration order."""
        return list(self._schemas)

    def describe(self, ns: str) -> dict[str, Any]:
        """One redacted namespace view for a form client."""
        if ns not in self._schemas:
            raise KeyError(f'settings namespace "{ns}" is not registered')
        schema = self._schemas[ns]
        envelope = schema.to_json()
        uid = envelope.get("uid")
        resolved = _defaults(
            envelope, uid, _merge(self._base.get(ns, {}), self._user.get(ns, {}))
        )
        value, secrets = _redact(envelope, uid, _project(envelope, uid, resolved), [])
        base, _unused = _redact(
            envelope,
            uid,
            _project(envelope, uid, _defaults(envelope, uid, self._base.get(ns, {}))),
            [],
        )
        user, _unused = _redact(
            envelope, uid, _project(envelope, uid, self._user.get(ns, {})), []
        )
        return {
            "ns": ns,
            "schema": _plain_schema(envelope),
            "value": value,
            "base": base,
            "user": user,
            "secrets": secrets,
            "revision": self._revisions[ns],
            "applies": "live",
            "autoGenerate": self._auto[ns],
        }

    def list(self) -> dict[str, Any]:
        """All namespaces plus deployment write facts."""
        editor = self.ctx.get("configEditor", None, strict=False)
        return {
            "writable": True,
            "hasDocument": editor is not None,
            "namespaces": [self.describe(ns) for ns in self.namespaces()],
        }

    async def replace(
        self, ns: str, value: Any, expected_revision: int
    ) -> dict[str, Any]:
        """Replace a namespace's user section wholesale."""
        return await self._write(ns, expected_revision, lambda _current: value)

    async def patch(
        self, ns: str, ops: builtins.list[Mapping[str, Any]], expected_revision: int
    ) -> dict[str, Any]:
        """Apply path-addressed edits to a namespace's user section."""

        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            result = current
            for op in ops:
                result = _apply_op(result, op)
            return result

        return await self._write(ns, expected_revision, mutate)

    async def _write(
        self,
        ns: str,
        expected_revision: int,
        mutation: Callable[[dict[str, Any]], Any],
    ) -> dict[str, Any]:
        if ns not in self._schemas:
            raise KeyError(f'settings namespace "{ns}" is not registered')
        async with self._lock:
            actual = self._revisions[ns]
            if expected_revision != actual:
                raise SettingsConflictError(ns, expected_revision, actual)
            candidate = _json_safe(mutation(copy.deepcopy(self._user.get(ns, {}))))
            if not isinstance(candidate, dict):
                raise TypeError("Config root must be a plain object")
            editor = self.ctx.get("configEditor", None, strict=False)
            if editor is not None:
                entry = next(
                    (item for item in editor.entries() if item.options.get("id") == ns),
                    None,
                )
                if entry is None:
                    raise KeyError(f'no profile entry for settings namespace "{ns}"')
                envelope = self._schemas[ns].to_json()

                def change(
                    raw: dict[str, Any], inherited: dict[str, Any]
                ) -> dict[str, Any]:
                    del inherited
                    return cast(
                        dict[str, Any],
                        _merge(
                            _strip_form(envelope, envelope.get("uid"), raw), candidate
                        ),
                    )

                await editor.edit(entry, change)
            self._user[ns] = candidate
            self._revisions[ns] = actual + 1
            revision = self._revisions[ns]
            self._emit(ns, revision)
            return self.describe(ns)

    def _emit(self, ns: str, revision: int) -> None:
        listeners = list(self.ctx.events._listeners.get(SETTINGS_UPDATED, []))
        for listener in listeners:
            try:
                returned = listener.callback(ns, revision)
                if inspect.isawaitable(returned):
                    task = asyncio.ensure_future(returned)
                    task.add_done_callback(
                        lambda done: done.exception() if not done.cancelled() else None
                    )
            except Exception as error:
                self.ctx.logger.warning(
                    f"settings: a {SETTINGS_UPDATED} listener failed: {error}"
                )


__all__ = ["SETTINGS_UPDATED", "SettingsConflictError", "SettingsService"]
