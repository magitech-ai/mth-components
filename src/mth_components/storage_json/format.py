"""On-disk JSON unit format for both storage layouts.

Ports ``packages/storage/storage-json/src/format.ts``: the whole-unit document
is pretty-printed and version-stamped; per-record documents carry the unit
version plus one value. A foreign per-record document reads as absent instead
of failing the unit.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from mth_components.storage.service import StorageError

#: Sentinel for an absent/foreign per-record document (distinct from a stored null).
MISSING = object()


@dataclass
class UnitState:
    """Authoritative in-memory state; the file is only its projection."""

    version: int
    global_: Any = None
    tables: dict[str, dict[str, Any]] = field(default_factory=dict)


def _json_safe(value: Any) -> Any:
    """Mirror ``JSON.stringify``: non-finite numbers become ``null``."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def serialize(name: str, state: UnitState) -> str:
    """Serialize one whole unit with a trailing newline."""
    document = {
        "unit": {"name": name, "version": state.version},
        "global": _json_safe(state.global_),
        "tables": _json_safe(state.tables),
    }
    return json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def parse(text: str, descriptor: Any) -> UnitState:
    """Parse a whole-unit document, validating identity, shape and version."""
    name = descriptor["name"]
    try:
        document = json.loads(text)
    except (ValueError, TypeError) as error:
        raise StorageError(
            "malformed-medium", f"unit '{name}': file is not valid JSON"
        ) from error
    if not isinstance(document, dict):
        raise StorageError(
            "malformed-medium", f"unit '{name}': file is not a JSON object"
        )
    unit = document.get("unit")
    if (
        not isinstance(unit, dict)
        or unit.get("name") != name
        or not isinstance(unit.get("version"), int)
        or isinstance(unit.get("version"), bool)
    ):
        raise StorageError(
            "malformed-medium", f"unit '{name}': missing or foreign unit header"
        )
    version = unit["version"]
    accepted = [descriptor["version"], *(descriptor.get("compatibleVersions") or ())]
    if version not in accepted:
        raise StorageError(
            "version-mismatch",
            f"unit '{name}': stored version {version} is not accepted by version "
            f"{descriptor['version']}",
        )
    tables = document.get("tables")
    if not isinstance(tables, dict):
        raise StorageError(
            "malformed-medium", f"unit '{name}': tables is not an object"
        )
    state = UnitState(version=version, global_=document.get("global"))
    for table in descriptor["tables"]:
        if table not in tables:
            state.tables[table] = {}
            continue
        records = tables[table]
        if not isinstance(records, dict):
            raise StorageError(
                "malformed-medium", f"unit '{name}': table '{table}' is not an object"
            )
        state.tables[table] = dict(records)
    return state


def serialize_record(version: int, value: Any) -> str:
    """Serialize one per-record document with a trailing newline."""
    document = {"version": version, "record": _json_safe(value)}
    return json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def parse_record(text: str, versions: tuple[int, ...]) -> Any:
    """Parse one record document; foreign documents read as absent."""
    try:
        document = json.loads(text)
    except (ValueError, TypeError):
        return MISSING
    if not isinstance(document, dict):
        return MISSING
    stamped = document.get("version")
    if not isinstance(stamped, int) or isinstance(stamped, bool):
        return MISSING
    if stamped not in versions or "record" not in document:
        return MISSING
    return document["record"]
