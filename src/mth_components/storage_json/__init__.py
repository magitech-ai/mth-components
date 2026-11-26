"""JSON storage backend component."""

from __future__ import annotations

from mth_components.storage_json.backend import (
    JsonStorageBackend,
    register_json_backend,
    validate_descriptor,
)
from mth_components.storage_json.format import (
    MISSING,
    UnitState,
    parse,
    parse_record,
    serialize,
    serialize_record,
)
from mth_components.storage_json.per_record import SAFE_KEY_RE, PerRecordJsonUnit
from mth_components.storage_json.single import SingleJsonUnit

__all__ = [
    "MISSING",
    "SAFE_KEY_RE",
    "JsonStorageBackend",
    "PerRecordJsonUnit",
    "SingleJsonUnit",
    "UnitState",
    "parse",
    "parse_record",
    "register_json_backend",
    "serialize",
    "serialize_record",
    "validate_descriptor",
]
