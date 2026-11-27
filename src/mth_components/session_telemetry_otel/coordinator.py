"""On-demand capture coordinator for session telemetry.

Ports the on-demand path of ``packages/session/session-telemetry/src/coordinator.ts``:
one logical record per canonical Session event, a per-session handoff cursor,
the ``session-telemetry/record`` redaction waterfall, and contained handoff to
the backend. Live capture is not mounted — the OTel backend selects on-demand
capture, so no continuous firehose or operational records exist here.
"""

from __future__ import annotations

import copy
import weakref
from collections.abc import Awaitable, Callable, Mapping
from functools import partial
from typing import Any, Protocol, TypedDict

from animas import Context

SessionTelemetrySeverity = str
SessionTelemetrySharingStatus = str

# Module-scope, keyed by Session: a re-adopting fiber resumes after its cursor
# instead of re-handing history. Entries die with their sessions.
_handoff: weakref.WeakKeyDictionary[Any, int] = weakref.WeakKeyDictionary()


class SessionTelemetryRecord(TypedDict, total=False):
    sourceEvent: dict[str, Any]
    channel: str
    time: int
    severity: SessionTelemetrySeverity
    attributes: dict[str, Any]
    body: Any


class SessionTelemetrySink(Protocol):
    """Minimum backend contract required by the coordinator."""

    def emit(self, record: SessionTelemetryRecord) -> None: ...

    def shutdown(self) -> Awaitable[None]: ...


class SessionTelemetryCoordinator:
    """Copy, redact and hand over a session's canonical log on request."""

    def __init__(
        self,
        ctx: Context,
        backend: SessionTelemetrySink,
        *,
        include_history: bool = False,
    ) -> None:
        self._ctx = ctx
        self._backend = backend
        self._include_history = include_history

        def factory() -> Callable[[], Awaitable[None]]:
            return self._dispose

        ctx.effect(factory, "telemetry capture")

    async def _dispose(self) -> None:
        try:
            await self._backend.shutdown()
        except Exception as error:
            self._ctx.logger.warning(f"telemetry: backend shutdown failed: {error}")

    async def capture_session(
        self, session: Any, through_seq: int | None = None
    ) -> None:
        """Hand over the canonical suffix after the handoff cursor."""
        start = getattr(session, "first_lifecycle_seq", 0)
        cursor = _handoff.get(session)
        if cursor is None:
            cursor = -1 if self._include_history or start == 0 else start - 1
        events: list[Any] = session.snapshot_events(cursor + 1)
        for event in events:
            if through_seq is not None and event.seq > through_seq:
                break
            await self._contain(partial(self._capture_event, session, event))

    async def _contain(self, step: Callable[[], Awaitable[None]]) -> None:
        """One rejected record is withheld without starving the replay."""
        try:
            await step()
        except Exception as error:
            self._ctx.logger.warning(f"telemetry: capture step failed: {error}")

    async def _capture_event(self, session: Any, event: Any) -> None:
        data = copy.deepcopy(event.payload)
        envelope = {
            "id": event.id,
            "sessionId": session.session_id,
            "type": event.type,
            "time": event.timestamp,
            "surfaceOp": event.surface_op,
            "seq": event.seq,
            "ignorable": event.ignorable,
            "sourceEventSeqs": event.source_event_seqs,
        }
        record: SessionTelemetryRecord = {
            "sourceEvent": {"sessionId": session.session_id, "envelope": envelope},
            "channel": "ledger",
            "time": event.timestamp,
            "severity": severity_of(event),
            "attributes": identity_of(session, event),
            "body": data,
        }
        redacted = await self._ctx.waterfall(
            "session-telemetry/record", record, default_handler=lambda item: item
        )
        self._backend.emit(redacted)
        _handoff[session] = event.seq


def severity_of(event: Any) -> SessionTelemetrySeverity:
    """Map an event's own outcome flag onto the alerting severity."""
    if event.type == "tool/result":
        message = event.payload.get("message")
        if isinstance(message, dict) and message.get("isError") is True:
            return "error"
        if event.payload.get("isError") is True:
            return "error"
    if (
        event.type == "turn/end"
        and event.payload.get("reason", {}).get("kind") == "error"
    ):
        return "error"
    return "info"


def identity_of(session: Any, event: Any) -> dict[str, Any]:
    """Build the minimal envelope identity attributes for one event."""
    header: Mapping[str, Any] = session.header
    attributes: dict[str, Any] = {
        "session.id": header.get("id", session.session_id),
        "session.format_version": header.get("version", 0),
        "event.type": event.type,
        "event.seq": event.seq,
    }
    if isinstance(header.get("cwd"), str):
        attributes["session.cwd"] = header["cwd"]
    if header.get("parentSession") is not None:
        attributes["session.parent_id"] = header["parentSession"]
    if header.get("isSeeded") is True:
        attributes["session.seed_length"] = session.inherited_event_count
    return attributes


def is_feedback(session: Any, event: Any) -> bool:
    """Only this session's explicit feedback authorizes replay."""
    if event.seq < session.inherited_event_count:
        return False
    if event.type == "feedback/record":
        return True
    if event.type in ("feedback/message-put", "feedback/message-delete"):
        return bool(event.payload.get("sessionId") == session.session_id)
    return False


__all__ = [
    "SessionTelemetryCoordinator",
    "SessionTelemetryRecord",
    "SessionTelemetrySeverity",
    "SessionTelemetrySharingStatus",
    "SessionTelemetrySink",
    "identity_of",
    "is_feedback",
    "severity_of",
]
