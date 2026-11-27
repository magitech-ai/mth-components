"""Feedback-bounded OTLP session telemetry backend.

Ports ``packages/session/session-telemetry-otel/src/index.ts``: only an
explicit, canonical feedback submission authorizes capture of the session
prefix; the reporter owns byte-bounded delivery and this backend owns resource
identity and the outer shutdown deadline. ``DISABLED`` constructs no SDK state.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Mapping
from enum import StrEnum
from typing import Any, ClassVar, cast
from urllib.parse import urlparse

from animas import Context, Service
from magitech_harness import __version__
from magitech_harness.session.events import SessionEvent
from magitech_harness.session.store import Session

from mth_components.otel import SessionLogReporter
from mth_components.session_telemetry_otel.coordinator import (
    SessionTelemetryCoordinator,
    SessionTelemetryRecord,
    is_feedback,
)
from mth_components.session_telemetry_otel.identity import (
    get_or_create_anonymous_user_id,
)

PRODUCT_NAME = "magitech-harness"
COMPONENT_VERSION = "0.1.0"
DEFAULT_TELEMETRY_MODE = "FEEDBACK_ONLY"
DEFAULT_SHUTDOWN_TIMEOUT_MILLIS = 3_000
MAX_TIMER_DELAY_MILLIS = 2_147_483_647
DISABLED_FEEDBACK_WARNING = (
    "OpenTelemetry session upload is DISABLED; this feedback is not uploaded "
    "through OpenTelemetry"
)
NON_CANONICAL_EVENT_WARNING = (
    "session telemetry ignored an event absent from the canonical session log"
)

_SEVERITY = {"info": 9, "warn": 13, "error": 17}


def _is_finite(value: int | float) -> bool:
    """``Number.isFinite`` semantics, including oversized Python ints."""
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class SessionTelemetryMode(StrEnum):
    FEEDBACK_ONLY = "FEEDBACK_ONLY"
    DISABLED = "DISABLED"


def resolve_mode(mode: Any) -> SessionTelemetryMode:
    """Resolve the default and reject unknown runtime values."""
    if mode is None:
        mode = DEFAULT_TELEMETRY_MODE
    try:
        return SessionTelemetryMode(mode)
    except ValueError as error:
        raise ValueError(
            f"session-telemetry-otel: unsupported mode {json.dumps(mode)}"
        ) from error


def sharing_status_for(mode: SessionTelemetryMode) -> str:
    """Map the mode onto the backend-independent sharing vocabulary."""
    if mode is SessionTelemetryMode.FEEDBACK_ONLY:
        return "feedback-only"
    return "disabled"


class OpenTelemetrySessionBackend(Service):
    """The only telemetry backend a deployment loads; duplicate loads throw."""

    inject: ClassVar[list[str]] = ["sessions", "otel"]

    def __init__(self, ctx: Context, config: Mapping[str, Any] | None = None) -> None:
        if ctx.get("sessionTelemetry", None, strict=False) is not None:
            raise RuntimeError("session telemetry backend already registered")
        cfg = dict(config or {})
        mode = resolve_mode(cfg.get("mode"))
        if mode is SessionTelemetryMode.DISABLED:
            super().__init__(ctx, "sessionTelemetry")
            self.sharing = sharing_status_for(mode)
            self._reporter: SessionLogReporter | None = None
            self._shutdown_timeout: float = float(DEFAULT_SHUTDOWN_TIMEOUT_MILLIS)
            ctx.on("session/event", self._on_disabled_event)
            return

        url = (cfg.get("exporter") or {}).get("url")
        if not isinstance(url, str) or not url:
            raise ValueError(
                "session-telemetry-otel: exporter.url is required (the full "
                "OTLP logs endpoint)"
            )
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                "session-telemetry-otel: exporter.url must be a valid http(s) "
                f"URL: {url!r}"
            )
        processor = dict(cfg.get("processor") or {})
        batch = processor.get("maxExportBatchSize")
        if batch is not None and (
            isinstance(batch, bool) or not isinstance(batch, int) or batch < 1
        ):
            raise ValueError(
                f"session-telemetry-otel: processor.maxExportBatchSize must be a "
                f"positive integer, got {batch!r}"
            )
        timeout = cfg.get("shutdownTimeoutMillis", DEFAULT_SHUTDOWN_TIMEOUT_MILLIS)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not _is_finite(timeout)
            or timeout <= 0
            or timeout > MAX_TIMER_DELAY_MILLIS
        ):
            raise ValueError(
                "session-telemetry-otel: shutdownTimeoutMillis must be a positive "
                f"finite number no greater than {MAX_TIMER_DELAY_MILLIS}, "
                f"got {timeout!r}"
            )
        super().__init__(ctx, "sessionTelemetry")
        self.sharing = sharing_status_for(mode)
        self._shutdown_timeout = float(timeout)
        self._reporter = SessionLogReporter(
            {
                "scope": {
                    "name": "mth_components.session_telemetry_otel",
                    "version": COMPONENT_VERSION,
                },
                "exporter": {**dict(cfg.get("exporter") or {}), "url": url},
                **({"processor": processor} if processor else {}),
                **(
                    {"maxRequestBytes": cfg["maxRequestBytes"]}
                    if "maxRequestBytes" in cfg
                    else {}
                ),
                "resourceAttributes": {
                    "service.name": PRODUCT_NAME,
                    "service.version": __version__,
                    "user.id": get_or_create_anonymous_user_id(),
                },
                "onFailure": lambda message, error: ctx.logger.warning(
                    f"session-telemetry-otel: {message}"
                    + (f": {error}" if error else "")
                ),
            }
        )
        self._coordinator: SessionTelemetryCoordinator = SessionTelemetryCoordinator(
            ctx, _Sink(self), include_history=True
        )
        ctx.on("session/event", self._on_session_event)
        ctx.on("feedback/committed", self._on_feedback_committed)

    def _on_disabled_event(self, session: Any, event: Any) -> None:
        if is_feedback(session, event):
            self.ctx.logger.warning(DISABLED_FEEDBACK_WARNING)

    async def _on_session_event(self, session: Any, event: Any) -> None:
        if not is_feedback(session, event):
            return
        if session.event_at(event.seq) is not event:
            self.ctx.logger.warning(NON_CANONICAL_EVENT_WARNING)
            return
        await self._coordinator.capture_session(session, event.seq)

    async def _on_feedback_committed(self, inspection: Any) -> None:
        snapshot = json.loads(json.dumps(inspection, default=str))
        committed_events = snapshot.get("events") or []
        if not committed_events:
            return
        meta = dict(snapshot.get("meta") or {})
        if not meta.get("version"):
            meta["version"] = 4
        seed = [_restore_event(item) for item in committed_events]
        session = Session(
            str(meta.get("id", "")),
            seed=seed,
            header=meta,
            inherited_event_count=int(snapshot.get("inheritedEventCount", 0)),
            event_state="detached",
            projections=self._message_projections(),
        )
        committed = session.events[-1]
        if is_feedback(session, committed):
            await self._coordinator.capture_session(session, committed.seq)

    def _message_projections(self) -> list[Any]:
        """Best-effort access to the message projections used by session restore."""
        sessions = self.ctx.get("sessions", None, strict=False)
        store = getattr(sessions, "store", None)
        return list(getattr(store, "message_projections", []) or [])

    def emit(self, record: SessionTelemetryRecord) -> None:
        """Direct records are dropped: only canonical feedback may be exported."""
        del record

    def _emit_record(self, record: SessionTelemetryRecord) -> None:
        """Hand one captured record to the byte-bounded reporter."""
        if self._reporter is None:
            return
        source = record.get("sourceEvent")
        if source is None:
            self.ctx.logger.warning(
                "Session log record withheld: redaction removed sourceEvent"
            )
            return
        severity = _SEVERITY.get(record.get("severity", "info"), 9)
        self._reporter.report_session_log(
            {
                "sessionId": source["sessionId"],
                "event": {**source["envelope"], "data": record.get("body")},
                "severityNumber": severity,
                "attributes": dict(record.get("attributes") or {}),
            }
        )

    async def shutdown(self) -> None:
        """Drain until the deployment deadline, then abandon queued records."""
        if self._reporter is None:
            return
        reporter = self._reporter
        provider_shutdown = reporter.shutdown()

        async def deadline() -> None:
            await asyncio.sleep(self._shutdown_timeout / 1000)
            reporter.stop_pending()
            raise TimeoutError(
                "session-telemetry-otel: provider shutdown exceeded "
                f"{self._shutdown_timeout}ms"
            )

        task = asyncio.create_task(deadline())
        try:
            await _race(provider_shutdown, task)
        finally:
            task.cancel()


class _Sink:
    """Private backend sink; the public service ``emit`` stays a no-op."""

    def __init__(self, backend: OpenTelemetrySessionBackend) -> None:
        self._backend = backend

    def emit(self, record: SessionTelemetryRecord) -> None:
        self._backend._emit_record(record)

    def shutdown(self) -> Awaitable[None]:
        return self._backend.shutdown()


def _restore_event(raw: Any) -> SessionEvent:
    """Map a serialized inspection event onto the harness SessionEvent shape."""
    aliases = {
        "sessionId": "session_id",
        "time": "timestamp",
        "data": "payload",
        "surfaceOp": "surface_op",
        "sourceEventSeqs": "source_event_seqs",
    }
    mapped = {aliases.get(key, key): value for key, value in dict(raw).items()}
    return SessionEvent(**cast(dict[str, Any], mapped))


async def _race(primary: asyncio.Future[None], deadline: asyncio.Task[None]) -> None:
    primary_task = asyncio.ensure_future(primary)
    done, _pending = await asyncio.wait(
        {primary_task, deadline}, return_when=asyncio.FIRST_COMPLETED
    )
    if primary_task in done:
        primary_task.result()
        return
    deadline.result()


__all__ = [
    "COMPONENT_VERSION",
    "DEFAULT_SHUTDOWN_TIMEOUT_MILLIS",
    "PRODUCT_NAME",
    "OpenTelemetrySessionBackend",
    "SessionTelemetryMode",
    "resolve_mode",
    "sharing_status_for",
]
