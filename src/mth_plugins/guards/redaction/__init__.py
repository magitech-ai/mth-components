"""Tool-output redaction guard component package."""

from mth_plugins.guards.redaction.component import apply
from mth_plugins.guards.redaction.redaction import (
    REDACTION_MARKER,
    SECRET_ENV_NAMES,
    redact,
    secret_values,
)

__all__ = [
    "REDACTION_MARKER",
    "SECRET_ENV_NAMES",
    "apply",
    "redact",
    "secret_values",
]
