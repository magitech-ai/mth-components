"""Shared OTel service (`ctx.otel`).

Ports ``packages/telemetry/otel/src/index.ts``: mounting creates no queue,
identity, or network connection. Consumers create independent reporter
channels and own their disposal.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from animas import Context, Service

from mth_components.otel.event_log import EventLogReporter
from mth_components.otel.session_log import SessionLogReporter


class OtelService(Service):
    """Creates independent ordinary-event and Session-log reporter channels."""

    def __init__(self, ctx: Context) -> None:
        if ctx.get("otel", None, strict=False) is not None:
            raise RuntimeError("otel service already registered")
        super().__init__(ctx, "otel")

    def create_event_reporter(self, options: Mapping[str, Any]) -> EventLogReporter:
        """Create an ordinary-event channel owned by the caller."""
        return EventLogReporter(options)

    def create_session_log_reporter(
        self, options: Mapping[str, Any]
    ) -> SessionLogReporter:
        """Create a byte-bounded Session-log channel owned by the caller."""
        return SessionLogReporter(options)

    async def check(self) -> None:
        """Lifecycle check: the service is healthy once constructed."""
        return None


__all__ = ["OtelService"]
