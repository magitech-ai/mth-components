"""Official catalog projection.

Ports ``packages/boot/plugin-manager/src/catalog.ts``: a bundle's localized
display fields collapsed to one catalog row.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _localized(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        english = value.get("en")
        return english if isinstance(english, str) else None
    return None


def bundle_catalog_entry(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Project one bundle into a catalog row."""
    raw_meta = bundle.get("meta")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    title = _localized(meta.get("title")) or str(bundle["name"])
    description = (
        _localized(meta.get("description"))
        or _localized(bundle.get("description"))
        or ""
    )
    return {
        "name": str(bundle["name"]),
        "title": title,
        "description": description,
        "enabled": bool(bundle.get("enabled", False)),
    }


__all__ = ["bundle_catalog_entry"]
