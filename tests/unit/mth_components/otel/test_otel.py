"""Unit tests for independent OTLP reporting channels."""

from __future__ import annotations

import asyncio
import gzip
import json
from collections.abc import Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, ClassVar

import pytest
from animas import Context

from mth_components.otel import (
    SESSION_LOG_MAX_REQUEST_BYTES,
    Cancellation,
    EventLogReporter,
    LogExporter,
    OtelService,
    SessionLogReporter,
    resolve_session_log_limits,
)
from mth_components.otel import transport as module
from mth_components.otel.component import apply
from mth_components.otel.session_log import MAX_PROCESSOR_VALUE, SessionLogProcessor
from mth_components.otel.transport import (
    ERROR,
    FAILED,
    INFO,
    SUCCESS,
    WARN,
    serialize_request,
    severity_text,
)


class _Collector(BaseHTTPRequestHandler):
    captures: ClassVar[list[tuple[dict[str, str], bytes]]] = []
    status = 200
    large_body: ClassVar[bool] = False

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        if self.headers.get("content-encoding") == "gzip":
            body = gzip.decompress(body)
        self.captures.append((dict(self.headers), body))
        self.send_response(self.status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b"x" * (4 * 1024 * 1024 + 1) if self.large_body else b"{}")

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture()
def collector() -> Any:
    _Collector.captures = []
    _Collector.status = 200
    _Collector.large_body = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1/logs", _Collector
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


class _FakeExporter:
    def __init__(
        self, size: int = 10, fail: Exception | None = None, code: str = SUCCESS
    ) -> None:
        self.size = size
        self.fail = fail
        self.code = code
        self.calls: list[Sequence[Mapping[str, Any]]] = []
        self.flushes = 0
        self.shutdowns = 0
        self.on_export: Any = None

    def serialized_bytes(self, records: Sequence[Mapping[str, Any]]) -> int:
        return self.size

    def serialize(self, records: Sequence[Mapping[str, Any]]) -> bytes:
        return b"x"

    async def export(
        self, records: Sequence[Mapping[str, Any]]
    ) -> tuple[str, Exception | None]:
        self.calls.append(records)
        if self.on_export is not None:
            self.on_export()
        if self.fail is not None:
            raise self.fail
        return self.code, None if self.code == SUCCESS else RuntimeError(
            "export failed"
        )

    async def force_flush(self) -> None:
        self.flushes += 1

    async def shutdown(self) -> None:
        self.shutdowns += 1


class _SlowExporter(_FakeExporter):
    async def export(
        self, records: Sequence[Mapping[str, Any]]
    ) -> tuple[str, Exception | None]:
        await asyncio.sleep(0.05)
        return SUCCESS, None


def _options(url: str, **overrides: Any) -> dict[str, Any]:
    return {
        "exporter": {
            "url": url,
            "timeoutMillis": 1000,
            **overrides.pop("exporter", {}),
        },
        "resourceAttributes": {"service.name": "test"},
        "scope": {"name": "scope", "version": "1.0"},
        "onFailure": lambda _message, _error: None,
        **overrides,
    }


def test_severity_and_serialization() -> None:
    assert severity_text(INFO) == "INFO"
    assert severity_text(WARN) == "WARN"
    assert severity_text(ERROR) == "ERROR"
    assert severity_text(42) == "42"
    payload = serialize_request(
        [
            {
                "eventName": "e",
                "body": "b",
                "timestamp": 5,
                "observedTimestamp": 7,
                "severityNumber": WARN,
                "attributes": {
                    "s": "x",
                    "i": 2,
                    "f": 1.5,
                    "b": True,
                    "m": {"k": "v"},
                    "a": [1, "z"],
                    "o": object(),
                },
            },
            {"body": "second", "severityNumber": 99},
        ],
        {"service.name": "svc"},
        {"name": "s", "version": "1"},
    )
    request = json.loads(payload)
    record = request["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert record["timeUnixNano"] == "5000000"
    assert record["observedTimeUnixNano"] == "7000000"
    assert record["severityText"] == "WARN"
    values = {a["key"]: a["value"] for a in record["attributes"]}
    assert values["i"] == {"intValue": 2}
    assert values["b"] == {"boolValue": True}
    assert values["m"]["kvlistValue"]["values"][0]["key"] == "k"
    assert values["a"]["arrayValue"]["values"][1] == {"stringValue": "z"}
    assert values["o"] == {}
    assert (
        request["resourceLogs"][0]["resource"]["attributes"][0]["key"] == "service.name"
    )
    assert request["resourceLogs"][0]["scopeLogs"][0]["scope"] == {
        "name": "s",
        "version": "1",
    }
    assert (
        "attributes" not in request["resourceLogs"][0]["scopeLogs"][0]["logRecords"][1]
    )


def test_resolve_limits() -> None:
    assert resolve_session_log_limits({}) == SESSION_LOG_MAX_REQUEST_BYTES
    assert resolve_session_log_limits({"maxRequestBytes": 100}) == 100
    for bad in (True, "x", 0, SESSION_LOG_MAX_REQUEST_BYTES + 1, 1.5):
        with pytest.raises(ValueError, match="maxRequestBytes"):
            resolve_session_log_limits({"maxRequestBytes": bad})
    for key in (
        "maxQueueSize",
        "maxExportBatchSize",
        "scheduledDelayMillis",
        "exportTimeoutMillis",
    ):
        for bad in (True, "x", 0, MAX_PROCESSOR_VALUE + 1):
            with pytest.raises(ValueError, match=f"processor.{key}"):
                resolve_session_log_limits({"processor": {key: bad}})
    with pytest.raises(ValueError, match="must not exceed"):
        resolve_session_log_limits(
            {"processor": {"maxQueueSize": 1, "maxExportBatchSize": 2}}
        )


@pytest.mark.asyncio
async def test_http_transport(collector: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("mth_components.otel.transport._sleep", _instant_sleep)
    url, handler = collector
    exporter = LogExporter(
        {
            "url": url,
            "compression": "gzip",
            "headers": lambda: {"x-extra": "v"},
            "userAgent": "ua",
            "timeoutMillis": 1000,
        },
        {"service.name": "svc"},
        {"name": "scope"},
    )
    assert exporter.serialized_bytes([{"body": "b"}]) > 0
    assert gzip.decompress(exporter.serialize([{"body": "b"}])) == serialize_request(
        [{"body": "b"}], {"service.name": "svc"}, {"name": "scope"}
    )
    code, error = await exporter.export([{"body": "b"}])
    assert (code, error) == (SUCCESS, None)
    headers, body = handler.captures[-1]
    assert headers["x-extra"] == "v" and headers["user-agent"] == "ua"
    assert (
        headers["content-encoding"] == "gzip"
        and headers["content-type"] == "application/json"
    )
    assert (
        json.loads(body)["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["body"][
            "stringValue"
        ]
        == "b"
    )
    await exporter.force_flush()
    await exporter.shutdown()
    await exporter.shutdown()
    assert (await exporter.export([]))[0] == FAILED

    with pytest.raises(ValueError, match="url"):
        LogExporter({})
    with pytest.raises(ValueError, match="url"):
        LogExporter({"url": 42})  # type: ignore[typeddict-item]

    handler.status = 503
    failing = LogExporter({"url": url})
    code, error = await failing.export([{"body": "b"}])
    assert code == FAILED and "503" in str(error)
    assert len(handler.captures) >= 7
    await failing.shutdown()


@pytest.mark.asyncio
async def test_http_transport_network_failure() -> None:
    exporter = LogExporter({"url": "http://127.0.0.1:1/v1/logs", "timeoutMillis": 100})
    code, error = await exporter.export([{"body": "b"}])
    assert code == FAILED and error is not None
    await exporter.shutdown()


@pytest.mark.asyncio
async def test_session_processor_rejections_and_thresholds() -> None:
    warnings: list[tuple[str, Exception | None]] = []
    warn = lambda message, error: warnings.append((message, error))  # noqa: E731
    exporter = _FakeExporter(size=10)
    processor = SessionLogProcessor(
        exporter,
        25,
        {
            "maxQueueSize": 2,
            "maxExportBatchSize": 5,
            "scheduledDelayMillis": 10000,
            "exportTimeoutMillis": 10000,
        },
        warn,
    )
    processor.on_emit({"body": "a"})
    processor.on_emit({"body": "b"})
    processor.on_emit({"body": "c"})
    assert "queue is full" in warnings[0][0]
    await processor.force_flush()
    assert len(exporter.calls) == 1 and len(exporter.calls[0]) == 2

    oversize = _FakeExporter(size=26)
    large = SessionLogProcessor(
        oversize,
        25,
        {
            "maxQueueSize": 10,
            "maxExportBatchSize": 10,
            "scheduledDelayMillis": 10000,
            "exportTimeoutMillis": 10000,
        },
        warn,
    )
    large.on_emit({"body": "big"})
    assert "content was not truncated" in warnings[-1][0]
    await large.force_flush()
    assert oversize.calls == []

    class Broken(_FakeExporter):
        def serialized_bytes(self, records: Sequence[Mapping[str, Any]]) -> int:
            raise RuntimeError("bad serialize")

    broken = SessionLogProcessor(
        Broken(),
        25,
        {
            "maxQueueSize": 10,
            "maxExportBatchSize": 10,
            "scheduledDelayMillis": 10000,
            "exportTimeoutMillis": 10000,
        },
        warn,
    )
    broken.on_emit({"body": "x"})
    assert "serialization failed" in warnings[-1][0]

    byte_batch = _FakeExporter(size=10)
    by_bytes = SessionLogProcessor(
        byte_batch,
        25,
        {
            "maxQueueSize": 10,
            "maxExportBatchSize": 10,
            "scheduledDelayMillis": 10000,
            "exportTimeoutMillis": 10000,
        },
        warn,
    )
    by_bytes.on_emit({"body": "1"})
    by_bytes.on_emit({"body": "2"})
    by_bytes.on_emit({"body": "3"})
    await asyncio.sleep(0)
    assert len(byte_batch.calls) == 2 and len(byte_batch.calls[0]) == 2

    count_batch = _FakeExporter(size=1)
    by_count = SessionLogProcessor(
        count_batch,
        25,
        {
            "maxQueueSize": 10,
            "maxExportBatchSize": 2,
            "scheduledDelayMillis": 10000,
            "exportTimeoutMillis": 10000,
        },
        warn,
    )
    by_count.on_emit({"body": "1"})
    by_count.on_emit({"body": "2"})
    await asyncio.sleep(0)
    assert len(count_batch.calls) == 1


@pytest.mark.asyncio
async def test_session_processor_timer_and_active() -> None:
    exporter = _FakeExporter(size=1)
    processor = SessionLogProcessor(
        exporter,
        25,
        {
            "maxQueueSize": 10,
            "maxExportBatchSize": 10,
            "scheduledDelayMillis": 1,
            "exportTimeoutMillis": 10000,
        },
        lambda _m, _e: None,
    )
    processor.on_emit({"body": "timer"})
    await asyncio.sleep(0.02)
    assert len(exporter.calls) == 1
    await processor.force_flush()
    processor.on_emit({"body": "while-active"})
    await processor.force_flush()
    assert len(exporter.calls) == 2
    await processor.shutdown()
    await processor.shutdown()
    assert exporter.shutdowns == 1


@pytest.mark.asyncio
async def test_session_processor_send_failures_and_watchdog() -> None:
    warnings: list[str] = []
    warn = lambda message, _error: warnings.append(message)  # noqa: E731
    failing = _FakeExporter(code=FAILED)
    processor = SessionLogProcessor(
        failing,
        25,
        {
            "maxQueueSize": 10,
            "maxExportBatchSize": 10,
            "scheduledDelayMillis": 10000,
            "exportTimeoutMillis": 0,
        },
        warn,
    )
    processor.on_emit({"body": "a"})
    await processor.force_flush()
    assert warnings[-1] == "Session log export failed"

    raising = _FakeExporter(fail=RuntimeError("boom"))
    processor2 = SessionLogProcessor(
        raising,
        25,
        {
            "maxQueueSize": 10,
            "maxExportBatchSize": 10,
            "scheduledDelayMillis": 10000,
            "exportTimeoutMillis": 10000,
        },
        warn,
    )
    processor2.on_emit({"body": "a"})
    await processor2.force_flush()
    assert warnings[-1] == "Session log export failed"

    empty_error = _FakeExporter(code=FAILED)
    empty_error.export = lambda records: (FAILED, None)  # type: ignore[method-assign]
    processor3 = SessionLogProcessor(
        empty_error,
        25,
        {
            "maxQueueSize": 10,
            "maxExportBatchSize": 10,
            "scheduledDelayMillis": 10000,
            "exportTimeoutMillis": 10000,
        },
        warn,
    )
    processor3.on_emit({"body": "a"})
    await processor3.force_flush()
    assert warnings[-1] == "Session log export failed"

    processor4 = SessionLogProcessor(
        _SlowExporter(),
        25,
        {
            "maxQueueSize": 10,
            "maxExportBatchSize": 10,
            "scheduledDelayMillis": 10000,
            "exportTimeoutMillis": 0,
        },
        warn,
    )
    processor4.on_emit({"body": "a"})
    await processor4.force_flush()
    await asyncio.sleep(0.01)
    assert any("exportTimeoutMillis" in item for item in warnings)


@pytest.mark.asyncio
async def test_session_reporter_and_stop_pending(collector: Any) -> None:
    url, handler = collector
    reporter = SessionLogReporter(
        _options(url, processor={"scheduledDelayMillis": 10000})
    )
    reporter.report_session_log(
        {
            "sessionId": "s1",
            "event": {
                "type": "user/message",
                "seq": 1,
                "time": 1800000000000,
                "data": {"text": "hello"},
            },
            "attributes": {"extra": "v"},
        }
    )
    reporter.report_session_log(
        {"sessionId": "s2", "event": {"type": "x", "time": 2}, "severityNumber": ERROR}
    )
    await reporter.shutdown()
    records = [
        record
        for _headers, body in handler.captures
        for record in json.loads(body)["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    ]
    assert len(records) == 2
    assert (
        records[0]["eventName"] == "session-log"
        and records[0]["severityText"] == "INFO"
    )
    assert records[1]["severityText"] == "ERROR"
    attrs = {a["key"]: a["value"]["stringValue"] for a in records[0]["attributes"]}
    assert attrs["sessionId"] == "s1" and attrs["extra"] == "v"
    assert json.loads(attrs["content"])["data"] == {"text": "hello"}

    reporter2 = SessionLogReporter(
        _options(url, processor={"scheduledDelayMillis": 10000})
    )
    reporter2.report_session_log({"sessionId": "s3", "event": {"type": "x", "time": 3}})
    reporter2.stop_pending()
    await reporter2.shutdown()
    assert len(handler.captures) == 1


@pytest.mark.asyncio
async def test_event_reporter(collector: Any) -> None:
    url, handler = collector
    reporter = EventLogReporter(_options(url, processor={"maxExportBatchSize": 1}))
    reporter.emit(
        {"eventName": "e", "body": "b", "timestamp": 1, "attributes": {"k": "v"}}
    )
    await reporter.shutdown()
    records = [
        record
        for _headers, body in handler.captures
        for record in json.loads(body)["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    ]
    assert len(records) == 1 and records[0]["eventName"] == "e"
    assert records[0]["severityNumber"] == INFO
    assert records[0]["observedTimeUnixNano"] != records[0]["timeUnixNano"]


@pytest.mark.asyncio
async def test_event_reporter_timer_active_and_abort(collector: Any) -> None:
    url, handler = collector
    reporter = EventLogReporter(_options(url, processor={"scheduledDelayMillis": 1}))
    reporter.emit({"body": "timer"})
    await asyncio.sleep(0.02)
    assert handler.captures
    await reporter.force_flush()
    reporter.emit({"body": "second"})
    await reporter.force_flush()
    assert len(handler.captures) == 2
    await reporter.shutdown()
    await reporter.shutdown()

    aborted = EventLogReporter(_options(url, processor={"scheduledDelayMillis": 10000}))
    aborted.emit({"body": "discarded"})
    await aborted.shutdown(lambda: True)
    assert len(handler.captures) == 2


@pytest.mark.asyncio
async def test_event_reporter_failure_paths() -> None:
    warnings: list[str] = []
    exporter = _FakeExporter(code=FAILED)
    reporter = EventLogReporter(
        _options("http://x/v1", processor={"maxExportBatchSize": 1})
    )
    reporter._exporter = exporter  # type: ignore[assignment]
    reporter._warn = lambda message, _error: warnings.append(message)  # type: ignore[assignment]
    reporter.emit({"body": "a"})
    await reporter.force_flush()
    assert warnings == ["Product telemetry export failed"]

    exporter.code = SUCCESS
    reporter.emit({"body": "b"})
    exporter.export = lambda records: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
    await reporter.force_flush()
    assert warnings[-1] == "Product telemetry export failed"

    shutdown_exporter = _FakeExporter()
    shutdown_exporter.shutdown = lambda: (_ for _ in ()).throw(RuntimeError("close"))  # type: ignore[method-assign]
    reporter._exporter = shutdown_exporter  # type: ignore[assignment]
    await reporter.shutdown()
    assert warnings[-1] == "Product telemetry exporter shutdown failed"


@pytest.mark.asyncio
async def test_service_and_component() -> None:
    ctx = Context()
    service = OtelService(ctx)
    assert ctx.otel is service
    with pytest.raises(RuntimeError, match="already registered"):
        OtelService(ctx)
    assert isinstance(
        service.create_event_reporter(_options("http://x/v1")), EventLogReporter
    )
    assert isinstance(
        service.create_session_log_reporter(_options("http://x/v1")), SessionLogReporter
    )
    await service.check()

    ctx2 = Context()
    await apply(ctx2)
    assert isinstance(ctx2.otel, OtelService)


@pytest.mark.asyncio
async def test_remaining_processor_branches() -> None:
    bare = serialize_request([{"timestamp": 1}])
    assert (
        "body"
        not in json.loads(bare)["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    )
    bare_resource = json.loads(bare)["resourceLogs"][0]["resource"]
    assert bare_resource == {"attributes": [], "droppedAttributesCount": 0}
    bare_record = json.loads(bare)["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert bare_record["droppedAttributesCount"] == 0 and "body" not in bare_record

    config = {
        "maxQueueSize": 10,
        "maxExportBatchSize": 10,
        "scheduledDelayMillis": 10000,
        "exportTimeoutMillis": 10000,
    }

    stopped = SessionLogProcessor(_SlowExporter(), 100, config, lambda _m, _e: None)
    stopped.stop_pending()
    stopped.on_emit({"body": "ignored"})
    assert stopped._queue == []

    no_timer = SessionLogProcessor(_SlowExporter(), 100, config, lambda _m, _e: None)
    no_timer.stop_pending()
    assert no_timer._timer is None

    active = SessionLogProcessor(
        _SlowExporter(size=50), 100, config, lambda _m, _e: None
    )
    active.on_emit({"body": "first"})
    active.on_emit({"body": "second"})
    assert active._active is not None
    active.on_emit({"body": "third"})
    assert active.force_flush() is active._active
    await active.force_flush()

    event_reporter = EventLogReporter(
        _options("http://x/v1", processor={"scheduledDelayMillis": 10000})
    )
    event_reporter.emit({"body": "first"})
    assert event_reporter._timer is not None and event_reporter._active is None
    event_reporter.emit({"body": "second"})
    assert event_reporter._active is None
    await event_reporter.shutdown(lambda: True)


@pytest.mark.asyncio
async def test_event_reporter_active_emit() -> None:
    reporter = EventLogReporter(
        _options("http://x/v1", processor={"maxExportBatchSize": 1})
    )
    reporter._exporter = _SlowExporter()  # type: ignore[assignment]
    reporter.emit({"body": "first"})
    assert reporter._active is not None
    reporter.emit({"body": "second"})
    await reporter.force_flush()


@pytest.mark.asyncio
async def test_transport_retry_response_limit_and_cancellation(
    collector: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _instant_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(module, "_sleep", _instant_sleep)

    with pytest.raises(ValueError, match="timeoutMillis"):
        LogExporter({"url": "http://x/v1", "timeoutMillis": 0})
    with pytest.raises(ValueError, match="timeoutMillis"):
        LogExporter({"url": "http://x/v1", "timeoutMillis": True})

    url, handler = collector
    handler.large_body = True
    exporter = LogExporter({"url": url})
    code, error = await exporter.export([{"body": "b"}])
    assert code == FAILED and "4 MiB" in str(error)
    await exporter.shutdown()

    handler.large_body = False
    rotation = iter(["first", "second"])

    def rotating() -> dict[str, str]:
        return {"x-rotate": next(rotation, "second")}

    rotating_exporter = LogExporter({"url": url, "headers": rotating})
    await rotating_exporter.export([{"body": "a"}])
    assert handler.captures[-1][0]["x-rotate"] == "first"
    await rotating_exporter.export([{"body": "b"}])
    assert handler.captures[-1][0]["x-rotate"] == "second"
    await rotating_exporter.shutdown()

    cancelled = Cancellation()
    cancelled_exporter = LogExporter({"url": url}, cancellation=cancelled)
    cancelled_exporter.cancel()
    code, error = await cancelled_exporter.export([{"body": "c"}])
    assert code == FAILED and "cancelled" in str(error)
    await cancelled_exporter.shutdown()

    async def _slow_post(_data: bytes) -> int:
        await asyncio.sleep(1)
        return 200

    live = LogExporter({"url": url}, cancellation=Cancellation())
    live._post_once = _slow_post  # type: ignore[method-assign]
    task = asyncio.create_task(live.export([{"body": "d"}]))
    await asyncio.sleep(0)
    live.cancel()
    code, error = await task
    assert code == FAILED and "cancelled" in str(error)

    no_signal = LogExporter({"url": url})
    no_signal.cancel()
    assert no_signal._active is None
    await no_signal.shutdown()


@pytest.mark.asyncio
async def test_event_reporter_new_paths() -> None:
    warnings: list[str] = []
    reporter = EventLogReporter(
        _options(
            "http://x/v1", processor={"maxQueueSize": 1, "scheduledDelayMillis": 10000}
        )
    )
    reporter._warn = lambda message, _error: warnings.append(message)  # type: ignore[assignment]
    reporter.emit({"body": "first"})
    reporter.emit({"body": "second"})
    assert warnings == ["Event log queue is full; record rejected"]
    await reporter.shutdown(lambda: True)

    timed = EventLogReporter(
        _options(
            "http://x/v1",
            processor={"exportTimeoutMillis": 1, "scheduledDelayMillis": 10000},
        )
    )
    timed._exporter = _SlowExporter()  # type: ignore[assignment]
    timed._warn = lambda message, _error: warnings.append(message)  # type: ignore[assignment]
    timed.emit({"body": "timed"})
    await timed.force_flush()
    assert "exportTimeoutMillis" in warnings[-1]

    active = EventLogReporter(
        _options(
            "http://x/v1",
            processor={"maxExportBatchSize": 1, "scheduledDelayMillis": 10000},
        )
    )
    active._exporter = _SlowExporter()  # type: ignore[assignment]
    active.emit({"body": "in-flight"})
    await asyncio.sleep(0)
    await active.shutdown(lambda: True)
    assert active._queue == []


def test_resolve_limits_integral_floats_and_nulls() -> None:
    assert (
        resolve_session_log_limits(
            {"processor": {"maxQueueSize": 3.0, "maxExportBatchSize": 1.0}}
        )
        == SESSION_LOG_MAX_REQUEST_BYTES
    )
    with pytest.raises(ValueError, match=r"processor\.maxQueueSize"):
        resolve_session_log_limits({"processor": {"maxQueueSize": None}})
    assert severity_text(5) == "DEBUG" and severity_text(21) == "FATAL"


@pytest.mark.asyncio
async def test_event_attributes_are_snapshotted() -> None:
    reporter = EventLogReporter(
        _options("http://x/v1", processor={"scheduledDelayMillis": 10000})
    )
    exporter = _FakeExporter()
    reporter._exporter = exporter  # type: ignore[assignment]
    attributes = {"k": "original", "nested": {"v": 1}}
    reporter.emit({"eventName": "e", "attributes": attributes})
    attributes["k"] = "mutated"
    attributes["nested"]["v"] = 2
    await reporter.force_flush()
    assert exporter.calls[0][0]["attributes"] == {
        "k": "original",
        "nested": {"v": 1},
    }
