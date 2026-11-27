"""Explicit OTLP JSON transport for telemetry channels.

Ports the wire contract of ``packages/telemetry/otel/src``: independent log
exporters that inherit no ambient credentials, send a JSON
``ExportLogsServiceRequest`` to one explicit endpoint, and never follow
redirects. Byte accounting uses the same serialized envelope as the wire, so a
rejected record is never split and an accepted batch stays within its ceiling.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, TypedDict

import httpx

OTLP_HTTP_LOG_EXPORTER_COMPONENT_TYPE = "otlp_http_log_exporter"

# SeverityNumber from @opentelemetry/api-logs.
_SEVERITY_NUMBERS = {
    0: "UNSPECIFIED",
    1: "TRACE",
    2: "TRACE2",
    3: "TRACE3",
    4: "TRACE4",
    5: "DEBUG",
    6: "DEBUG2",
    7: "DEBUG3",
    8: "DEBUG4",
    9: "INFO",
    10: "INFO2",
    11: "INFO3",
    12: "INFO4",
    13: "WARN",
    14: "WARN2",
    15: "WARN3",
    16: "WARN4",
    17: "ERROR",
    18: "ERROR2",
    19: "ERROR3",
    20: "ERROR4",
    21: "FATAL",
    22: "FATAL2",
    23: "FATAL3",
    24: "FATAL4",
}
INFO = 9
WARN = 13
ERROR = 17

SUCCESS = "SUCCESS"
FAILED = "FAILED"

RETRY_LIMIT = 5
RETRY_STATUS_CODES = frozenset({429, 502, 503, 504})
RETRY_BACKOFF_LIMIT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_MILLIS = 10_000

_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


class TransportFailureError(Exception):
    """A transport-level export failure surfaced through the callback."""


class LogExporterOptions(TypedDict, total=False):
    """Collector and SDK HTTP settings accepted by this package's exporters."""

    url: str
    timeoutMillis: int
    compression: str
    headers: dict[str, str] | Callable[[], dict[str, str]]
    userAgent: str


class Cancellation:
    """Channel-owned abort state checked before and between transport attempts."""

    def __init__(self) -> None:
        self._aborted = False

    def abort(self) -> None:
        self._aborted = True

    def is_aborted(self) -> bool:
        return self._aborted


def severity_text(number: int) -> str:
    """Map a severity number onto its OTel enum name."""
    return _SEVERITY_NUMBERS.get(number, str(number))


def _any_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": value}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, Mapping):
        return {"kvlistValue": {"values": [_attribute(k, v) for k, v in value.items()]}}
    if isinstance(value, Sequence):
        return {"arrayValue": {"values": [_any_value(item) for item in value]}}
    return {}


def _attribute(key: str, value: Any) -> dict[str, Any]:
    return {"key": key, "value": _any_value(value)}


def serialize_request(
    records: Sequence[Mapping[str, Any]],
    resource: Mapping[str, Any] | None = None,
    scope: Mapping[str, str] | None = None,
) -> bytes:
    """Serialize one OTLP JSON logs request in the SDK's wire shape.

    Each record is measured with the same resource/scope envelope it is
    exported with, so a single-record measurement bounds every multi-record
    request.
    """
    resource_log: dict[str, Any] = {
        "resource": {
            "attributes": [_attribute(k, v) for k, v in (resource or {}).items()],
            "droppedAttributesCount": 0,
        }
    }
    scope_log: dict[str, Any] = {}
    if scope is not None:
        scope_log["scope"] = dict(scope)
    encoded: list[dict[str, Any]] = []
    for record in records:
        severity = record.get("severityNumber", INFO)
        entry: dict[str, Any] = {
            "timeUnixNano": str(record.get("timestamp", 0) * 1_000_000),
            "observedTimeUnixNano": str(record.get("observedTimestamp", 0) * 1_000_000),
            "severityNumber": severity,
            "severityText": record.get("severityText", severity_text(severity)),
            "droppedAttributesCount": 0,
        }
        if "body" in record:
            entry["body"] = {"stringValue": record["body"]}
        if "eventName" in record:
            entry["eventName"] = record["eventName"]
        attributes = record.get("attributes")
        if attributes:
            entry["attributes"] = [_attribute(k, v) for k, v in attributes.items()]
        encoded.append(entry)
    scope_log["logRecords"] = encoded
    resource_log["scopeLogs"] = [scope_log]
    return json.dumps(
        {"resourceLogs": [resource_log]}, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


class LogExporter:
    """One channel-owned OTLP JSON log exporter."""

    def __init__(
        self,
        options: LogExporterOptions,
        resource: Mapping[str, Any] | None = None,
        scope: Mapping[str, str] | None = None,
        cancellation: Cancellation | None = None,
    ) -> None:
        url = options.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("otel: exporter.url must be a non-empty string")
        self.url = url
        timeout = options.get("timeoutMillis", DEFAULT_TIMEOUT_MILLIS)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
        ):
            raise ValueError(
                "otel: exporter.timeoutMillis must be a positive finite number"
            )
        self.timeout_seconds = float(timeout) / 1000
        self.compression = options.get("compression")
        self._headers = options.get("headers")
        self.user_agent = options.get("userAgent")
        self.resource = dict(resource or {})
        self.scope = dict(scope or {})
        self._cancellation = cancellation
        self._client = httpx.AsyncClient(
            follow_redirects=False, timeout=self.timeout_seconds, trust_env=False
        )
        self._closed = False
        self._active: asyncio.Task[int] | None = None

    def _headers_for_request(self) -> dict[str, str]:
        headers = self._headers
        base = headers() if callable(headers) else dict(headers or {})
        result = {"content-type": "application/json", **base}
        if self.compression == "gzip":
            result["content-encoding"] = "gzip"
        if self.user_agent is not None:
            result["user-agent"] = self.user_agent
        return result

    def serialized_bytes(self, records: Sequence[Mapping[str, Any]]) -> int:
        """Uncompressed byte size of one record in its export envelope."""
        return len(serialize_request(records, self.resource, self.scope))

    def serialize(self, records: Sequence[Mapping[str, Any]]) -> bytes:
        """Serialize records with this exporter's resource and scope."""
        data = serialize_request(records, self.resource, self.scope)
        return gzip.compress(data) if self.compression == "gzip" else data

    async def _post_once(self, data: bytes) -> int:
        async with self._client.stream(
            "POST", self.url, content=data, headers=self._headers_for_request()
        ) as response:
            if response.status_code < 300:
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        raise TransportFailureError("OTLP response exceeded 4 MiB")
            return response.status_code

    async def _send(self, data: bytes) -> int:
        attempt = 0
        while True:
            if self._cancellation is not None and self._cancellation.is_aborted():
                raise asyncio.CancelledError("otel export cancelled")
            if attempt > 0:
                await _sleep(min(RETRY_BACKOFF_LIMIT_SECONDS, 2.0 ** (attempt - 1)))
            status = await self._post_once(data)
            if status not in RETRY_STATUS_CODES or attempt == RETRY_LIMIT:
                return status
            attempt += 1

    async def export(
        self, records: Sequence[Mapping[str, Any]]
    ) -> tuple[str, Exception | None]:
        """Send one batch; return an OTel-style result code and failure."""
        if self._closed:
            return FAILED, TransportFailureError("exporter is shut down")
        task = asyncio.get_running_loop().create_task(
            self._send(self.serialize(records))
        )
        self._active = task
        try:
            status = await task
        except asyncio.CancelledError:
            return FAILED, TransportFailureError("otel export cancelled")
        except Exception as error:
            return FAILED, error
        finally:
            self._active = None
        if status >= 300:
            return FAILED, TransportFailureError(
                f"OTLP collector returned HTTP {status}"
            )
        return SUCCESS, None

    def cancel(self) -> None:
        """Cancel the current transport attempt and all future ones."""
        if self._cancellation is not None:
            self._cancellation.abort()
        if self._active is not None:
            self._active.cancel()

    async def force_flush(self) -> None:
        """The exporter holds no buffer of its own."""
        return None

    async def shutdown(self) -> None:
        """Release the exporter's owned HTTP client once."""
        if not self._closed:
            self._closed = True
            await self._client.aclose()


__all__ = [
    "ERROR",
    "FAILED",
    "INFO",
    "MAX_RESPONSE_BYTES",
    "OTLP_HTTP_LOG_EXPORTER_COMPONENT_TYPE",
    "RETRY_LIMIT",
    "RETRY_STATUS_CODES",
    "SUCCESS",
    "WARN",
    "Cancellation",
    "LogExporter",
    "LogExporterOptions",
    "TransportFailureError",
    "serialize_request",
    "severity_text",
]
