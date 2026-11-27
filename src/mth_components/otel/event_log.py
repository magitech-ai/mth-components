"""Ordinary-event reporting over a cancellable OTLP transport.

Ports ``packages/telemetry/otel/src/{event-log,event-transport}.ts``: a
caller-owned queue with count-based batching, a bounded queue and per-export
timeout, independently configured resource and scope, and a shutdown whose
abort signal cancels the active transport attempt. The channel shares no state
with any Session-log channel.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from typing import Any, TypedDict, cast

from mth_components.otel.transport import (
    FAILED,
    INFO,
    SUCCESS,
    Cancellation,
    LogExporter,
    LogExporterOptions,
    TransportFailureError,
    severity_text,
)

_DEFAULT_MAX_QUEUE_SIZE = 2048
_DEFAULT_MAX_EXPORT_BATCH_SIZE = 512
_DEFAULT_SCHEDULED_DELAY_MILLIS = 1000
_DEFAULT_EXPORT_TIMEOUT_MILLIS = 30_000


class OTelEventRecord(TypedDict, total=False):
    eventName: str
    body: str
    timestamp: int
    severityNumber: int
    attributes: dict[str, Any]


class EventLogOptions(TypedDict, total=False):
    exporter: LogExporterOptions
    resourceAttributes: dict[str, Any]
    scope: dict[str, str]
    processor: dict[str, int]
    onFailure: Callable[[str, Exception | None], None]


class EventLogReporter:
    """One caller-owned ordinary-event queue, independent of Session logs."""

    def __init__(self, options: Mapping[str, Any]) -> None:
        self._cancellation = Cancellation()
        self._exporter = LogExporter(
            cast(LogExporterOptions, dict(options["exporter"])),
            cast(dict[str, Any], dict(options.get("resourceAttributes") or {})),
            cast(dict[str, str], dict(options["scope"])),
            self._cancellation,
        )
        processor = cast(Mapping[str, int], options.get("processor") or {})
        self._batch = processor.get(
            "maxExportBatchSize", _DEFAULT_MAX_EXPORT_BATCH_SIZE
        )
        self._queue_limit = processor.get("maxQueueSize", _DEFAULT_MAX_QUEUE_SIZE)
        self._delay = processor.get(
            "scheduledDelayMillis", _DEFAULT_SCHEDULED_DELAY_MILLIS
        )
        self._timeout = (
            processor.get("exportTimeoutMillis", _DEFAULT_EXPORT_TIMEOUT_MILLIS) / 1000
        )
        self._warn = cast(
            Callable[[str, Exception | None], None],
            options.get("onFailure", lambda _message, _error: None),
        )
        self._queue: list[Mapping[str, Any]] = []
        self._timer: asyncio.TimerHandle | None = None
        self._active: asyncio.Future[None] | None = None
        self._shutdown_promise: asyncio.Future[None] | None = None
        self._stopped = False

    def emit(self, record: Mapping[str, Any]) -> None:
        if len(self._queue) >= self._queue_limit:
            self._warn("Event log queue is full; record rejected", None)
            return
        severity = cast(int, record.get("severityNumber", INFO))
        item: dict[str, Any] = {
            "observedTimestamp": int(time.time() * 1000),
            "severityNumber": severity,
            "severityText": severity_text(severity),
        }
        for key in ("eventName", "body", "timestamp", "attributes"):
            if key in record:
                item[key] = record[key]
        self._queue.append(item)
        if self._active is not None:
            return
        if len(self._queue) >= self._batch:
            self.force_flush()
        elif self._timer is None:
            self._timer = asyncio.get_running_loop().call_later(
                self._delay / 1000, self._on_timer
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
            while (
                not self._stopped
                and not self._cancellation.is_aborted()
                and self._queue
            ):
                batch = self._queue[: self._batch]
                del self._queue[: self._batch]
                try:
                    code, error = await asyncio.wait_for(
                        self._exporter.export(batch), self._timeout
                    )
                except TimeoutError:
                    self._warn("Event log export exceeded exportTimeoutMillis", None)
                    continue
                except Exception as caught:
                    code, error = FAILED, caught
                if code != SUCCESS:
                    self._warn(
                        "Product telemetry export failed",
                        error
                        or TransportFailureError(
                            "Product telemetry HTTP export failed"
                        ),
                    )
                await self._exporter.force_flush()
        finally:
            self._active = None

    def shutdown(
        self, aborted: Callable[[], bool] | None = None
    ) -> asyncio.Future[None]:
        if self._shutdown_promise is None:
            self._shutdown_promise = asyncio.get_running_loop().create_task(
                self._finish(aborted)
            )
        return self._shutdown_promise

    async def _finish(self, aborted: Callable[[], bool] | None) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if aborted is not None and aborted():
            self._cancellation.abort()
            self._stopped = True
            self._queue.clear()
            if self._active is not None:
                self._active.cancel()
        else:
            await self.force_flush()
        try:
            await self._exporter.shutdown()
        except Exception as error:
            self._warn("Product telemetry exporter shutdown failed", error)


__all__ = ["EventLogOptions", "EventLogReporter", "OTelEventRecord"]
