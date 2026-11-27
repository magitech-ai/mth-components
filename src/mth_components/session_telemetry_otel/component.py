"""Animas component entry point for OTLP session telemetry."""

from __future__ import annotations

from typing import Any

from animas import Context

from mth_components.session_telemetry_otel.backend import OpenTelemetrySessionBackend


async def apply(ctx: Context, **kwargs: Any) -> None:
    """Mount ``ctx.sessionTelemetry``."""
    ctx.use(OpenTelemetrySessionBackend, dict(kwargs))
    await ctx.flush()


apply.inject = ["sessions", "otel"]  # type: ignore[attr-defined]
