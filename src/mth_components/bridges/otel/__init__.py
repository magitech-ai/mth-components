"""OpenTelemetry bridge component package."""

from mth_components.bridges.otel.component import apply
from mth_components.bridges.otel.tracer import OtelTracerService, SpanRecord

__all__ = [
    "OtelTracerService",
    "SpanRecord",
    "apply",
]
