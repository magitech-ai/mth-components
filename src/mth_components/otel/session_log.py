"""Session-log records in an independent byte-bounded OTLP queue.

Ports ``packages/telemetry/otel/src/session-log.ts``: one in-flight transport
request, byte/count batching, a watchdog that warns without releasing that
slot, rejection of oversized records without truncating their content, and
shutdown that drains queued requests before releasing the transport.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypedDict, cast

from mth_components.otel.transport import (
    FAILED,
    INFO,
    SUCCESS,
    LogExporter,
    LogExporterOptions,
    TransportFailureError,
    severity_text,
)

SESSION_LOG_MAX_REQUEST_BYTES = 4_000_000
MAX_PROCESSOR_VALUE = 2_147_483_647
_DEFAULT_MAX_QUEUE_SIZE = 2048
_DEFAULT_MAX_EXPORT_BATCH_SIZE = 512
_DEFAULT_SCHEDULED_DELAY_MILLIS = 1000
_DEFAULT_EXPORT_TIMEOUT_MILLIS = 30_000

_LimitKeys = (
    "maxQueueSize",
    "maxExportBatchSize",
    "scheduledDelayMillis",
    "exportTimeoutMillis",
)


class SessionLogProcessorOptions(TypedDict, total=False):
    maxQueueSize: int
    maxExportBatchSize: int
    scheduledDelayMillis: int
    exportTimeoutMillis: int


class SessionLogRecord(TypedDict, total=False):
    sessionId: str
    event: dict[str, Any]
    attributes: dict[str, Any]
    severityNumber: int


class SessionLogOptions(TypedDict, total=False):
    exporter: LogExporterOptions
    processor: SessionLogProcessorOptions
    maxRequestBytes: int
    scope: dict[str, str]
    resourceAttributes: dict[str, Any]
    onFailure: Callable[[str, Exception | None], None]


def _safe_integer(value: Any) -> bool:
    """JS ``Number.isSafeInteger``: integral numbers, never booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return float(value).is_integer() and abs(value) <= MAX_PROCESSOR_VALUE


def resolve_session_log_limits(options: Mapping[str, Any]) -> int:
    """Validate byte and queue settings before constructing a pipeline."""
    limit = options.get("maxRequestBytes")
    if limit is None:
        limit = SESSION_LOG_MAX_REQUEST_BYTES
    if not _safe_integer(limit) or not 1 <= limit <= SESSION_LOG_MAX_REQUEST_BYTES:
        raise ValueError(
            "session log maxRequestBytes must be an integer between 1 and "
            f"{SESSION_LOG_MAX_REQUEST_BYTES}"
        )
    processor = options.get("processor") or {}
    for key in _LimitKeys:
        if key in processor:
            value = processor[key]
            if not _safe_integer(value) or not 1 <= value <= MAX_PROCESSOR_VALUE:
                raise ValueError(
                    f"session log processor.{key} must be a positive integer no "
                    f"greater than {MAX_PROCESSOR_VALUE}"
                )
    queue = processor.get("maxQueueSize", _DEFAULT_MAX_QUEUE_SIZE)
    batch = processor.get("maxExportBatchSize", _DEFAULT_MAX_EXPORT_BATCH_SIZE)
    if batch > queue:
        raise ValueError("session log maxExportBatchSize must not exceed maxQueueSize")
    return cast(int, limit)


@dataclass(frozen=True)
class _Queued:
    record: Mapping[str, Any]
    bytes: int


class SessionLogProcessor:
    """Byte/count batching with one transport request in flight."""

    def __init__(
        self,
        exporter: LogExporter,
        limit: int,
        config: Mapping[str, int],
        warn: Callable[[str, Exception | None], None],
    ) -> None:
        self._exporter = exporter
        self._limit = limit
        self._config = config
        self._warn = warn
        self._queue: list[_Queued] = []
        self._bytes = 0
        self._timer: asyncio.TimerHandle | None = None
        self._active: asyncio.Future[None] | None = None
        self._shutdown_promise: asyncio.Future[None] | None = None
        self._stopped = False

    def on_emit(self, record: Mapping[str, Any]) -> None:
        if self._stopped:
            return
        if len(self._queue) >= self._config["maxQueueSize"]:
            self._warn("Session log queue is full; record rejected", None)
            return
        try:
            size = self._exporter.serialized_bytes([record])
        except Exception as error:
            self._warn("Session log serialization failed; record rejected", error)
            return
        if size > self._limit:
            self._warn(
                "Session log record rejected; content was not truncated",
                ValueError(
                    f"Session log record exceeds maxRequestBytes: "
                    f"{size} > {self._limit}"
                ),
            )
            return
        self._queue.append(_Queued(record, size))
        self._bytes += size
        if self._active is not None:
            return
        if (
            len(self._queue) >= self._config["maxExportBatchSize"]
            or self._bytes >= self._limit
        ):
            self.force_flush()
        elif self._timer is None:
            self._timer = asyncio.get_running_loop().call_later(
                self._config["scheduledDelayMillis"] / 1000, self._on_timer
            )

    def _on_timer(self) -> None:
        self._timer = None
        self.force_flush()

    def force_flush(self) -> asyncio.Future[None]:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._active is not None:
            return self._active
        if not self._queue:
            future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            future.set_result(None)
            return future
        self._active = asyncio.get_running_loop().create_task(self._drain())
        return self._active

    async def _drain(self) -> None:
        try:
            while not self._stopped and self._queue:
                size = 0
                count = 0
                for entry in self._queue:
                    if (
                        count == self._config["maxExportBatchSize"]
                        or size + entry.bytes > self._limit
                    ):
                        break
                    size += entry.bytes
                    count += 1
                batch = [entry.record for entry in self._queue[:count]]
                del self._queue[:count]
                self._bytes -= size
                await self._send(batch)
                await self._exporter.force_flush()
        finally:
            self._active = None

    async def _send(self, records: Sequence[Mapping[str, Any]]) -> None:
        def watchdog() -> None:
            self._warn(
                "Session log request exceeded exportTimeoutMillis; "
                "waiting for transport settlement",
                None,
            )

        timer = asyncio.get_running_loop().call_later(
            self._config["exportTimeoutMillis"] / 1000, watchdog
        )
        try:
            try:
                code, error = await self._exporter.export(records)
            except Exception as caught:
                code, error = FAILED, caught
            if code != SUCCESS:
                self._warn(
                    "Session log export failed",
                    error or TransportFailureError("Session log HTTP export failed"),
                )
        finally:
            timer.cancel()

    def stop_pending(self) -> None:
        self._stopped = True
        self._queue.clear()
        self._bytes = 0
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def shutdown(self) -> asyncio.Future[None]:
        if self._shutdown_promise is None:
            self._shutdown_promise = asyncio.get_running_loop().create_task(
                self._finish()
            )
        return self._shutdown_promise

    async def _finish(self) -> None:
        await self.force_flush()
        await self._exporter.shutdown()


class SessionLogReporter:
    """Owns feedback-authorized Session logs; no product-event queue is mounted."""

    def __init__(self, options: Mapping[str, Any]) -> None:
        limit = resolve_session_log_limits(options)
        exporter_options = cast(LogExporterOptions, dict(options["exporter"]))
        scope = cast(dict[str, str], dict(options["scope"]))
        resource = cast(dict[str, Any], dict(options.get("resourceAttributes") or {}))
        processor_options = cast(Mapping[str, int], options.get("processor") or {})
        self._exporter = LogExporter(exporter_options, resource, scope)
        self._processor = SessionLogProcessor(
            self._exporter,
            limit,
            {
                "maxQueueSize": processor_options.get(
                    "maxQueueSize", _DEFAULT_MAX_QUEUE_SIZE
                ),
                "maxExportBatchSize": processor_options.get(
                    "maxExportBatchSize", _DEFAULT_MAX_EXPORT_BATCH_SIZE
                ),
                "scheduledDelayMillis": processor_options.get(
                    "scheduledDelayMillis", _DEFAULT_SCHEDULED_DELAY_MILLIS
                ),
                "exportTimeoutMillis": processor_options.get(
                    "exportTimeoutMillis", _DEFAULT_EXPORT_TIMEOUT_MILLIS
                ),
            },
            cast(Callable[[str, Exception | None], None], options["onFailure"]),
        )

    def report_session_log(self, record: Mapping[str, Any]) -> None:
        severity = cast(int, record.get("severityNumber", INFO))
        event = record["event"]
        attributes = {
            **dict(record.get("attributes") or {}),
            "sessionId": record["sessionId"],
            "content": json.dumps(event, separators=(",", ":"), ensure_ascii=False),
        }
        self._processor.on_emit(
            {
                "eventName": "session-log",
                "body": "session-log",
                "timestamp": event["time"],
                "observedTimestamp": event["time"],
                "severityNumber": severity,
                "severityText": severity_text(severity),
                "attributes": attributes,
            }
        )

    def stop_pending(self) -> None:
        self._processor.stop_pending()

    def shutdown(self) -> asyncio.Future[None]:
        return self._processor.shutdown()


__all__ = [
    "MAX_PROCESSOR_VALUE",
    "SESSION_LOG_MAX_REQUEST_BYTES",
    "SessionLogOptions",
    "SessionLogProcessor",
    "SessionLogProcessorOptions",
    "SessionLogRecord",
    "SessionLogReporter",
    "resolve_session_log_limits",
]
