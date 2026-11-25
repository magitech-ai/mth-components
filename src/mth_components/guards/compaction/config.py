"""Token-budget resolution for compaction (threshold and retain ratios).

Token budgets replace the old event-count heuristic. The numbers match the
reference compaction-basic defaults: threshold ratio 0.8, retain ratio 0.16,
headroom 65,536. Per-model overrides are matched by exact (provider, model).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_THRESHOLD_RATIO = 0.8
DEFAULT_RETAIN_RATIO = 0.16
DEFAULT_HEADROOM_TOKENS = 65_536
FALLBACK_CONTEXT_WINDOW = 1_000_000


@dataclass(frozen=True)
class ResolvedTargetPolicy:
    provider: str
    model: str
    threshold_ratio: float
    retain_ratio: float
    headroom_tokens: int


@dataclass(frozen=True)
class CompactSpec:
    threshold_tokens: int
    retain_tokens: int

    def __post_init__(self) -> None:
        if self.threshold_tokens <= 0 or self.retain_tokens <= 0:
            raise ValueError("compaction thresholds must be positive")
        if self.retain_tokens >= self.threshold_tokens:
            raise ValueError("retainTokens must be < thresholdTokens")


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _ratio(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a ratio in (0, 1]")
    if not 0 < value <= 1:
        raise ValueError(f"{label} must be a ratio in (0, 1]")
    return float(value)


def resolve_target_policy(
    config: Mapping[str, Any], provider: str, model: str
) -> ResolvedTargetPolicy:
    """Resolve exact (provider, model) overrides over the top-level defaults."""
    threshold = _ratio(
        config.get(
            "threshold_ratio", config.get("thresholdRatio", DEFAULT_THRESHOLD_RATIO)
        ),
        "thresholdRatio",
    )
    retain = _ratio(
        config.get("retain_ratio", config.get("retainRatio", DEFAULT_RETAIN_RATIO)),
        "retainRatio",
    )
    headroom = _positive_int(
        config.get(
            "headroom_tokens", config.get("headroomTokens", DEFAULT_HEADROOM_TOKENS)
        ),
        "headroomTokens",
    )
    if retain >= threshold:
        raise ValueError("retainRatio must be < thresholdRatio")
    for override in config.get("model_policies", config.get("modelPolicies", [])) or []:
        if not isinstance(override, Mapping):
            continue
        if override.get("provider") == provider and override.get("model") == model:
            threshold = _ratio(
                override.get("thresholdRatio", threshold), "thresholdRatio"
            )
            retain = _ratio(override.get("retainRatio", retain), "retainRatio")
            headroom = _positive_int(
                override.get("headroomTokens", headroom), "headroomTokens"
            )
            break
    return ResolvedTargetPolicy(provider, model, threshold, retain, headroom)


def resolve_compact_spec(
    policy: ResolvedTargetPolicy,
    context_window: int,
    reserved_completion_tokens: int = 0,
) -> CompactSpec:
    """Mirror the reference budget formulas from compactSpec."""
    context_window = _positive_int(context_window, "contextWindow")
    if (
        isinstance(reserved_completion_tokens, bool)
        or not isinstance(reserved_completion_tokens, int)
        or reserved_completion_tokens < 0
    ):
        raise ValueError("reservedCompletionTokens must be a non-negative integer")
    message_budget = context_window - reserved_completion_tokens
    if message_budget <= 0:
        raise ValueError("messageBudgetTokens must be positive")
    pressure_budget = message_budget - policy.headroom_tokens
    if pressure_budget <= 0:
        raise ValueError("pressureBudgetTokens must be positive")
    threshold_tokens = int(
        min(context_window * policy.threshold_ratio, pressure_budget)
    )
    retain_tokens = int(message_budget * policy.retain_ratio)
    return CompactSpec(threshold_tokens=threshold_tokens, retain_tokens=retain_tokens)


__all__ = [
    "DEFAULT_HEADROOM_TOKENS",
    "DEFAULT_RETAIN_RATIO",
    "DEFAULT_THRESHOLD_RATIO",
    "FALLBACK_CONTEXT_WINDOW",
    "CompactSpec",
    "ResolvedTargetPolicy",
    "resolve_compact_spec",
    "resolve_target_policy",
]
