"""Domain declaration vocabulary and spec validation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, NotRequired, TypedDict, cast

from mth_components.storage.service import UNIT_NAME_RE


class Validator:
    """A durable-boundary record validator (Python counterpart of a zod schema)."""

    def __init__(self, check: Callable[[Any], Any]) -> None:
        self._check = check

    def validate(self, value: Any) -> Any:
        return self._check(value)


class DomainGlobalSpec(TypedDict):
    """Global singleton declaration: validator plus pre-write initial value."""

    schema: Validator
    initial: Any


DomainTableSpec = TypedDict(
    "DomainTableSpec",
    {"valueSchema": Validator, "__key": NotRequired[str]},
)


DomainSpec = TypedDict(
    "DomainSpec",
    {
        "name": str,
        "version": int,
        "layout": str,
        "compatibleVersions": list[int],
        "invalidRecords": str,
        "global": DomainGlobalSpec,
        "tables": dict[str, DomainTableSpec],
    },
    total=False,
)


def domain_table(schema: Validator) -> DomainTableSpec:
    """Declare one table validated by *schema*."""
    return cast(DomainTableSpec, {"valueSchema": schema})


def define_domain(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a domain declaration at module load, before any medium is touched."""
    result = dict(spec)
    name = result.get("name")
    if not isinstance(name, str) or not UNIT_NAME_RE.fullmatch(name):
        raise ValueError(f"domain name '{name}' must match {UNIT_NAME_RE.pattern}")

    version = result.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValueError(
            f"domain '{name}' version must be a non-negative integer, got {version}"
        )
    for compat in result.get("compatibleVersions") or ():
        if (
            isinstance(compat, bool)
            or not isinstance(compat, int)
            or compat < 0
            or compat >= version
        ):
            raise ValueError(
                f"domain '{name}' compatibleVersions entries must be non-negative "
                f"integers below version {version}, got {compat}"
            )
    layout = result.get("layout")
    if layout is not None and layout not in ("single", "per-record"):
        raise ValueError(
            f"domain '{name}' layout must be 'single' or 'per-record', got {layout}"
        )
    policy = result.get("invalidRecords")
    if policy is not None and policy != "backup-and-skip":
        raise ValueError(
            f"domain '{name}' invalidRecords must be 'backup-and-skip' "
            f"when present, got {policy}"
        )
    tables = result.get("tables")
    if not isinstance(tables, dict):
        raise ValueError(f"domain '{name}' tables must be a mapping")
    for table in tables:
        if not isinstance(table, str) or not UNIT_NAME_RE.fullmatch(table):
            raise ValueError(
                f"domain '{name}' table name '{table}' must match "
                f"{UNIT_NAME_RE.pattern}"
            )
    global_spec = result.get("global")
    if global_spec is not None:
        try:
            global_spec["schema"].validate(None)
        except Exception:
            pass
        else:
            raise ValueError(
                f"domain '{name}' global schema must not accept null: null is the "
                'medium\'s "never written" sentinel'
            )
    return result


def descriptor_of(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Project a spec onto the backend-facing unit descriptor."""
    descriptor: dict[str, Any] = {
        "name": spec["name"],
        "version": spec["version"],
        "tables": list(spec["tables"]),
        "hasGlobal": spec.get("global") is not None,
    }
    if spec.get("layout") is not None:
        descriptor["layout"] = spec["layout"]
    if spec.get("compatibleVersions") is not None:
        descriptor["compatibleVersions"] = list(spec["compatibleVersions"])
    return descriptor


__all__ = [
    "DomainGlobalSpec",
    "DomainSpec",
    "DomainTableSpec",
    "Validator",
    "define_domain",
    "descriptor_of",
    "domain_table",
]
