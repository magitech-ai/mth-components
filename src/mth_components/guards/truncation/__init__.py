"""Output truncation guardrail component package."""

from mth_components.guards.truncation.component import apply
from mth_components.guards.truncation.truncation import (
    TruncationResult,
    format_size,
    truncate_head,
    truncate_tail,
)

__all__ = [
    "TruncationResult",
    "apply",
    "format_size",
    "truncate_head",
    "truncate_tail",
]
