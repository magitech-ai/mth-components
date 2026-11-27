"""Unit tests for feedback-bounded OTLP session telemetry."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from animas import Context
from magitech_harness.session.events import SessionEvent
from magitech_harness.session.store import Session

from mth_components.session_telemetry_otel import OpenTelemetrySessionBackend, identity
from mth_components.session_telemetry_otel import backend as backend_module
from mth_components.session_telemetry_otel import identity as identity_module
from mth_components.session_telemetry_otel.backend import (
    DISABLED_FEEDBACK_WARNING,
    NON_CANONICAL_EVENT_WARNING,
    SessionTelemetryMode,
    resolve_mode,
    sharing_status_for,
)
from mth_components.session_telemetry_otel.backend import (
    OpenTelemetrySessionBackend as Backend,
)
from mth_components.session_telemetry_otel.component import apply
from mth_components.session_telemetry_otel.coordinator import (
    SessionTelemetryCoordinator,
    identity_of,
    is_feedback,
    session_id_of,
    severity_of,
)


def _config(
    url: str = "http://127.0.0.1:1/v1/logs", **overrides: Any
) -> dict[str, Any]:
    return {
        "exporter": {"url": url, "timeoutMillis": 1000},
        "processor": {"scheduledDelayMillis": 100000},
        **overrides,
    }


def _event(seq: int, kind: str = "user/message", **payload: Any) -> SessionEvent:
    return SessionEvent(
        id=f"e{seq}",
        session_id="s1",
        type=kind,
        timestamp=1800000000000 + seq,
        payload=payload,
        seq=seq,
        surface_op="append" if kind in ("user/message", "tool/result") else None,
    )


def _session(events: list[SessionEvent], **header: Any) -> Session:
    return Session(
        "s1",
        seed=events,
        header={
            "version": 4,
            "id": "s1",
            "createdAt": 1800000000000,
            "isSeeded": False,
            "delegationDepth": 0,
            **header,
        },
        inherited_event_count=0,
        event_state="detached",
    )


class _FakeReporter:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.stopped = False
        self.shutdowns = 0

    def report_session_log(self, record: dict[str, Any]) -> None:
        self.records.append(record)

    def stop_pending(self) -> None:
        self.stopped = True

    async def shutdown(self) -> None:
        self.shutdowns += 1


@pytest.fixture(autouse=True)
def _clear_memo() -> Any:
    identity._memo.clear()
    yield
    identity._memo.clear()


def test_identity(tmp_path: Path) -> None:
    first = identity.get_or_create_anonymous_user_id(
        str(tmp_path),
        lambda: (
            "a" * 8 + "-" + "b" * 4 + "-" + "c" * 4 + "-" + "d" * 4 + "-" + "e" * 12
        ),
    )
    assert (tmp_path / identity.ANONYMOUS_USER_ID_FILE_NAME).read_text() == first + "\n"
    assert identity.get_or_create_anonymous_user_id(str(tmp_path)) == first

    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / identity.ANONYMOUS_USER_ID_FILE_NAME).write_text("not-a-uuid\n")
    second = identity.get_or_create_anonymous_user_id(str(corrupt), lambda: first)
    assert (
        second == first
        and (corrupt / identity.ANONYMOUS_USER_ID_FILE_NAME).read_text().strip()
        == first
    )

    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / identity.ANONYMOUS_USER_ID_FILE_NAME).write_text(first.upper() + "\n")
    assert identity.get_or_create_anonymous_user_id(str(existing)) == first.upper()

    concurrent = tmp_path / "concurrent"
    concurrent.mkdir()
    (concurrent / identity.ANONYMOUS_USER_ID_FILE_NAME).write_text(first + "\n")
    assert (
        identity.get_or_create_anonymous_user_id(str(concurrent), lambda: "x") == first
    )

    blocked = tmp_path / "blocked-file"
    blocked.write_text("not a directory")
    value = identity.get_or_create_anonymous_user_id(
        str(blocked / "child"), lambda: "z"
    )
    assert value == "z"

    reread = tmp_path / "reread"
    reread.mkdir()
    reads = iter([None, first])
    original_read = identity_module._read_persisted_id
    original_open = Path.open
    try:
        identity_module._read_persisted_id = lambda _path: next(reads)  # type: ignore[assignment]

        def failing_exclusive(self: Any, mode: str, *args: Any, **kwargs: Any) -> Any:
            if mode == "x":
                raise OSError("concurrent winner")
            return original_open(self, mode, *args, **kwargs)

        Path.open = failing_exclusive  # type: ignore[method-assign]
        assert (
            identity.get_or_create_anonymous_user_id(str(reread), lambda: "x") == first
        )
    finally:
        identity_module._read_persisted_id = original_read  # type: ignore[assignment]
        Path.open = original_open  # type: ignore[method-assign]


def test_mode_and_validation() -> None:
    assert resolve_mode(None) is SessionTelemetryMode.FEEDBACK_ONLY
    assert resolve_mode("DISABLED") is SessionTelemetryMode.DISABLED
    with pytest.raises(ValueError, match="unsupported mode"):
        resolve_mode("UNKNOWN")
    assert sharing_status_for(SessionTelemetryMode.FEEDBACK_ONLY) == "feedback-only"
    assert sharing_status_for(SessionTelemetryMode.DISABLED) == "disabled"

    ctx = Context()
    with pytest.raises(ValueError, match=r"exporter\.url is required"):
        Backend(ctx, {})
    with pytest.raises(ValueError, match="valid http"):
        Backend(ctx, _config("file:///tmp/x"))
    with pytest.raises(ValueError, match="maxExportBatchSize"):
        Backend(ctx, _config(processor={"maxExportBatchSize": 0}))
    with pytest.raises(ValueError, match="maxExportBatchSize"):
        Backend(ctx, _config(processor={"maxExportBatchSize": True}))
    with pytest.raises(ValueError, match="shutdownTimeoutMillis"):
        Backend(ctx, _config(shutdownTimeoutMillis=0))
    with pytest.raises(ValueError, match="shutdownTimeoutMillis"):
        Backend(ctx, _config(shutdownTimeoutMillis=True))
    with pytest.raises(ValueError, match="shutdownTimeoutMillis"):
        Backend(
            ctx,
            _config(shutdownTimeoutMillis=backend_module.MAX_TIMER_DELAY_MILLIS + 1),
        )


@pytest.mark.asyncio
async def test_disabled_mode() -> None:
    ctx = Context()
    warnings: list[str] = []
    ctx.logger.warning = warnings.append  # type: ignore[method-assign]
    backend = Backend(ctx, {"mode": "DISABLED"})
    assert backend.sharing == "disabled" and backend._reporter is None
    backend._emit_record(
        {"sourceEvent": {"sessionId": "s1", "envelope": {}}, "body": None}
    )
    assert backend.emit({"sourceEvent": {}, "body": None}) is None
    session = _session([_event(0, "feedback/record")])
    ctx.emit("session/event", session, session.events[0])
    assert warnings == [DISABLED_FEEDBACK_WARNING]
    ctx.emit("session/event", session, _event(1, "user/message"))
    assert len(warnings) == 1
    await backend.shutdown()


@pytest.mark.asyncio
async def test_coordinator_capture_and_waterfall() -> None:
    ctx = Context()
    backend = Backend(ctx, _config())
    fake = _FakeReporter()
    backend._reporter = fake  # type: ignore[assignment]

    events = [
        _event(0),
        _event(1, "tool/result", isError=True),
        _event(2, "turn/end", reason={"kind": "error"}),
    ]
    session = _session(events, cwd="/tmp/work", parentSession="p1", isSeeded=False)
    await backend._coordinator.capture_session(session)
    assert len(fake.records) == 3
    assert fake.records[0]["event"]["type"] == "user/message"
    assert fake.records[1]["event"]["data"] == {"isError": True}
    assert fake.records[1]["severityNumber"] == 17
    assert fake.records[2]["severityNumber"] == 17
    assert fake.records[0]["event"]["time"] == events[0].timestamp

    # Cursor advanced: a second capture hands nothing over.
    await backend._coordinator.capture_session(session)
    assert len(fake.records) == 3

    # Redaction waterfall transforms the record before handoff.
    async def redact(record: dict[str, Any], next_fn: Any) -> dict[str, Any]:
        transformed = await next_fn(record)
        transformed["attributes"] = {"redacted": True}
        return transformed

    ctx.on("session-telemetry/record", redact)
    session2 = _session([_event(0)])
    await backend._coordinator.capture_session(session2)
    assert fake.records[-1]["attributes"] == {"redacted": True}

    # A throwing waterfall listener withholds the record, contained.
    warnings: list[str] = []
    ctx.logger.warning = warnings.append  # type: ignore[method-assign]
    ctx.on(
        "session-telemetry/record",
        lambda record, next_fn: (_ for _ in ()).throw(RuntimeError("rule")),
    )
    session3 = _session([_event(0)])
    await backend._coordinator.capture_session(session3)
    assert any("capture step failed" in item for item in warnings)


@pytest.mark.asyncio
async def test_coordinator_cursor_and_through_seq() -> None:
    ctx = Context()
    backend = Backend(ctx, _config())
    fake = _FakeReporter()
    backend._reporter = fake  # type: ignore[assignment]

    coordinator = SessionTelemetryCoordinator(
        ctx, backend_module._Sink(backend), include_history=False
    )
    session = _session([_event(0), _event(1), _event(2)])
    session.first_lifecycle_seq = 1
    await coordinator.capture_session(session, 1)
    assert len(fake.records) == 1
    await coordinator.capture_session(session)
    assert len(fake.records) == 2

    history = SessionTelemetryCoordinator(
        ctx, backend_module._Sink(backend), include_history=True
    )
    session2 = _session([_event(0), _event(1)])
    session2.first_lifecycle_seq = 1
    await history.capture_session(session2)
    assert len(fake.records) == 4

    start_zero = SessionTelemetryCoordinator(
        ctx, backend_module._Sink(backend), include_history=False
    )
    session3 = _session([_event(0)])
    session3.first_lifecycle_seq = 0
    await start_zero.capture_session(session3)
    assert len(fake.records) == 5


def test_coordinator_helpers() -> None:
    assert severity_of(_event(0, "tool/result", message={"isError": True})) == "error"
    assert severity_of(_event(0, "tool/result", message={"isError": False})) == "info"
    assert severity_of(_event(0, "tool/result", isError=True)) == "error"
    assert severity_of(_event(0, "turn/end", reason={"kind": "error"})) == "error"
    assert severity_of(_event(0, "turn/end", reason={"kind": "stop"})) == "info"
    assert severity_of(_event(0)) == "info"

    seeded = _session([_event(0)], isSeeded=True, cwd="/x", parentSession="p")
    seeded.inherited_event_count = 3
    attrs = identity_of(seeded, _event(1))
    assert attrs["session.cwd"] == "/x"
    assert attrs["session.parent_id"] == "p"
    assert attrs["session.seed_length"] == 3
    plain = identity_of(_session([_event(0)]), _event(0))
    assert "session.cwd" not in plain and "session.seed_length" not in plain

    assert is_feedback(_session([_event(0)]), _event(0, "feedback/record"))
    message = _event(0, "feedback/message-put", sessionId="s1")
    assert is_feedback(_session([message]), message)
    other = _event(0, "feedback/message-put", sessionId="other")
    assert not is_feedback(_session([other]), other)
    assert not is_feedback(_session([_event(0)]), _event(0, "user/message"))
    inherited = _session([_event(0, "feedback/record")])
    inherited.inherited_event_count = 1
    assert not is_feedback(inherited, inherited.events[0])


@pytest.mark.asyncio
async def test_backend_emit_and_events() -> None:
    ctx = Context()
    backend = Backend(ctx, _config())
    fake = _FakeReporter()
    backend._reporter = fake  # type: ignore[assignment]
    warnings: list[str] = []
    ctx.logger.warning = warnings.append  # type: ignore[method-assign]

    backend._emit_record(
        {"sourceEvent": None, "channel": "ledger", "severity": "info", "body": None}
    )
    assert any("withheld" in item for item in warnings)
    backend._emit_record(
        {
            "sourceEvent": {
                "sessionId": "s1",
                "envelope": {"type": "x", "time": 1, "seq": 0},
            },
            "channel": "ledger",
            "severity": "warn",
            "attributes": {"k": "v"},
            "body": {"text": "t"},
        }
    )
    assert fake.records[-1]["severityNumber"] == 13
    assert fake.records[-1]["event"]["data"] == {"text": "t"}

    session = _session([_event(0, "user/message"), _event(1, "feedback/record")])
    ctx.emit("session/event", session, session.events[0])
    await asyncio.sleep(0)
    assert len(fake.records) == 1
    ctx.emit("session/event", session, session.events[1])
    await asyncio.sleep(0)
    assert len(fake.records) == 3
    foreign = _event(2, "feedback/record")
    ctx.emit("session/event", session, foreign)
    await asyncio.sleep(0)
    assert any(NON_CANONICAL_EVENT_WARNING in item for item in warnings)
    assert len(fake.records) == 3

    inspection = {
        "meta": {
            "id": "s2",
            "createdAt": 10,
            "isSeeded": False,
            "delegationDepth": 0,
        },
        "events": [
            {
                "id": "f0",
                "session_id": "s2",
                "type": "feedback/record",
                "timestamp": 10,
                "payload": {},
                "seq": 0,
            }
        ],
        "inheritedEventCount": 0,
    }
    await backend._on_feedback_committed(inspection)
    assert fake.records[-1]["event"]["sessionId"] == "s2"
    before_empty = len(fake.records)
    await backend._on_feedback_committed(
        {"meta": {}, "events": [], "inheritedEventCount": 0}
    )
    assert len(fake.records) == before_empty
    no_version = {
        "meta": {
            "id": "s3",
            "createdAt": 10,
            "isSeeded": False,
            "delegationDepth": 0,
        },
        "events": [
            {
                "id": "f0",
                "session_id": "s3",
                "type": "feedback/record",
                "timestamp": 10,
                "payload": {},
                "seq": 0,
            }
        ],
        "inheritedEventCount": 0,
    }
    await backend._on_feedback_committed(no_version)
    assert fake.records[-1]["event"]["sessionId"] == "s3"
    non_feedback = {
        "meta": {
            "id": "s4",
            "version": 4,
            "createdAt": 10,
            "isSeeded": False,
            "delegationDepth": 0,
        },
        "events": [
            {
                "id": "n0",
                "session_id": "s4",
                "type": "user/message",
                "timestamp": 10,
                "payload": {},
                "seq": 0,
                "surface_op": "append",
            }
        ],
        "inheritedEventCount": 0,
    }
    await backend._on_feedback_committed(non_feedback)
    assert fake.records[-1]["event"]["sessionId"] == "s3"


@pytest.mark.asyncio
async def test_shutdown_deadline() -> None:
    ctx = Context()
    backend = Backend(ctx, _config(shutdownTimeoutMillis=10))
    slow = _FakeReporter()

    async def slow_shutdown() -> None:
        await asyncio.sleep(0.2)

    slow.shutdown = slow_shutdown  # type: ignore[method-assign]
    backend._reporter = slow  # type: ignore[assignment]
    with pytest.raises(TimeoutError, match="shutdown exceeded"):
        await backend.shutdown()
    assert slow.stopped

    quick = _FakeReporter()
    backend._reporter = quick  # type: ignore[assignment]
    await backend.shutdown()
    assert quick.shutdowns == 1


@pytest.mark.asyncio
async def test_component_apply() -> None:
    ctx = Context()
    ctx.provide("sessions", object())
    ctx.provide("otel", object())
    await apply(ctx, exporter={"url": "http://127.0.0.1:1/v1/logs"})
    assert isinstance(ctx.sessionTelemetry, OpenTelemetrySessionBackend)


@pytest.mark.asyncio
async def test_duplicate_load_and_teardown_effect() -> None:
    ctx = Context()
    backend = Backend(ctx, _config())
    with pytest.raises(RuntimeError, match="already registered"):
        Backend(ctx, _config())
    assert backend._reporter is not None
    exporter = backend._reporter._exporter
    assert exporter._closed is False
    await ctx.fiber.dispose()
    assert exporter._closed is True

    ctx2 = Context()
    backend2 = Backend(ctx2, _config())
    warnings: list[str] = []
    ctx2.logger.warning = warnings.append  # type: ignore[method-assign]

    async def failing_shutdown() -> None:
        raise RuntimeError("close failed")

    backend2.shutdown = failing_shutdown  # type: ignore[method-assign]
    await ctx2.fiber.dispose()
    assert any("backend shutdown failed" in item for item in warnings)


def test_header_authoritative_session_id() -> None:
    event = _event(0, "feedback/message-put", sessionId="header-id")
    session = Session(
        "store-id",
        seed=[event],
        header={
            "version": 4,
            "id": "header-id",
            "createdAt": 1800000000000,
            "isSeeded": False,
            "delegationDepth": 0,
        },
        inherited_event_count=0,
        event_state="detached",
    )
    assert session_id_of(session) == "header-id"
    assert session_id_of(type("S", (), {"session_id": "store-id"})()) == "store-id"
    assert identity_of(session, event)["session.id"] == "header-id"
    assert is_feedback(session, event)

    mismatched = _event(0, "feedback/message-put", sessionId="store-id")
    assert not is_feedback(session, mismatched)


def test_oversized_timeout_is_value_error() -> None:
    ctx = Context()
    with pytest.raises(ValueError, match="shutdownTimeoutMillis"):
        Backend(ctx, _config(shutdownTimeoutMillis=10**400))
