"""Tool-call timeout policy guardrail component package."""

from __future__ import annotations

from mth_plugins.guards.timeout_policy.component import apply
from mth_plugins.guards.timeout_policy.policy import TOOL_TIMEOUT, TimeoutPolicy

__all__ = [
    "TOOL_TIMEOUT",
    "TimeoutPolicy",
    "apply",
]
