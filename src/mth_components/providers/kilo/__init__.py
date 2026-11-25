"""Kilo Code keyless model provider component for Magitech Harness."""

from __future__ import annotations

from mth_components.providers.kilo.component import apply
from mth_components.providers.kilo.provider import (
    DEFAULT_MODEL,
    KiloAdapter,
    KiloProvider,
    parse_event_line,
    wire_model_id,
)

__all__ = [
    "DEFAULT_MODEL",
    "KiloAdapter",
    "KiloProvider",
    "apply",
    "parse_event_line",
    "wire_model_id",
]
