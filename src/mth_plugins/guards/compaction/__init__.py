"""Context compaction guard component for Magitech Harness."""

from __future__ import annotations

from mth_plugins.guards.compaction.compaction import (
    compact_session,
    estimate_history_tokens,
    estimate_tokens,
    find_safe_cut_index,
    summarize_events,
)
from mth_plugins.guards.compaction.component import apply
from mth_plugins.guards.compaction.config import (
    CompactSpec,
    ResolvedTargetPolicy,
    resolve_compact_spec,
    resolve_target_policy,
)

__all__ = [
    "CompactSpec",
    "ResolvedTargetPolicy",
    "apply",
    "compact_session",
    "estimate_history_tokens",
    "estimate_tokens",
    "find_safe_cut_index",
    "resolve_compact_spec",
    "resolve_target_policy",
    "summarize_events",
]
