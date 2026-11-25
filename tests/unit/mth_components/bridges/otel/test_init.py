"""Tests for OpenTelemetry bridge package exports."""

import mth_components.bridges.otel as otel_pkg


def test_init_exports() -> None:
    assert hasattr(otel_pkg, "OtelTracerService")
    assert hasattr(otel_pkg, "SpanRecord")
    assert hasattr(otel_pkg, "apply")
