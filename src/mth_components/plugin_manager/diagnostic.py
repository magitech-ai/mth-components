"""Validated operation diagnostic records.

Ports ``packages/boot/plugin-manager/src/diagnostic.ts``: only whitelisted
fields cross the diagnostic transport; malformed records are dropped.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError


class OperationCommon(BaseModel):
    id: UUID
    time: datetime
    reason: Literal["plugin", "bundle", "install", "remove"]
    target: str | None = Field(default=None, max_length=256)
    stage: Literal["install", "enable", "remove"]
    enabled: bool | None = None
    version: str | None = Field(default=None, max_length=128)


class OperationStarted(OperationCommon):
    phase: Literal["started"] = "started"


class OperationSettled(OperationCommon):
    phase: Literal["settled"] = "settled"
    application: Literal[
        "applied", "restart-required", "overridden", "failed", "cancelled"
    ]
    changed: bool
    errorCode: (  # noqa: N815
        Literal[
            "management-required",
            "unaddressable",
            "unknown-plugin",
            "invalid-spec",
            "already-installed",
            "ambiguous-install",
            "not-bundle",
            "not-removable",
            "stop-profile",
            "bundle-in-use",
            "stale-approval",
            "incompatible-version",
            "unsupported-platform",
            "unexpected-package",
            "operation-error",
        ]
        | None
    ) = None
    restartReason: (  # noqa: N815
        Literal[
            "no-hmr",
            "package-moved",
            "host-module",
            "unmanaged-plugin",
            "module-comparison",
        ]
        | None
    ) = None


class OperationUnknown(OperationCommon):
    phase: Literal["unknown"] = "unknown"


OperationRecord = OperationStarted | OperationSettled | OperationUnknown


class PluginOperationRecord(BaseModel):
    runId: UUID  # noqa: N815
    operation: OperationRecord


def parse_operation_record(value: Any) -> PluginOperationRecord | None:
    """Return a validated record, or ``None`` for anything off the whitelist."""
    try:
        return PluginOperationRecord.model_validate(value)
    except (ValidationError, ValueError, TypeError):
        return None


__all__ = [
    "OperationRecord",
    "OperationSettled",
    "OperationStarted",
    "OperationUnknown",
    "PluginOperationRecord",
    "parse_operation_record",
]
