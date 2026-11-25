"""Unit tests for compaction-basic budget resolution (reference formulas)."""

from __future__ import annotations

import pytest

from mth_components.guards.compaction.config import (
    DEFAULT_HEADROOM_TOKENS,
    DEFAULT_RETAIN_RATIO,
    DEFAULT_THRESHOLD_RATIO,
    FALLBACK_CONTEXT_WINDOW,
    CompactSpec,
    ResolvedTargetPolicy,
    resolve_compact_spec,
    resolve_target_policy,
)


def test_defaults() -> None:
    assert DEFAULT_THRESHOLD_RATIO == 0.8
    assert DEFAULT_RETAIN_RATIO == 0.16
    assert DEFAULT_HEADROOM_TOKENS == 65_536
    assert FALLBACK_CONTEXT_WINDOW == 1_000_000


def test_resolve_defaults() -> None:
    policy = resolve_target_policy({}, "p", "m")
    assert policy.threshold_ratio == 0.8
    assert policy.retain_ratio == 0.16
    assert policy.headroom_tokens == 65_536


def test_resolve_model_override() -> None:
    cfg = {
        "thresholdRatio": 0.7,
        "modelPolicies": [
            {
                "provider": "p",
                "model": "big",
                "thresholdRatio": 0.9,
                "retainRatio": 0.2,
                "headroomTokens": 1000,
            },
            {"provider": "x", "model": "y", "retainRatio": 0.1},
        ],
    }
    policy = resolve_target_policy(cfg, "p", "big")
    assert (policy.threshold_ratio, policy.retain_ratio, policy.headroom_tokens) == (
        0.9,
        0.2,
        1000,
    )
    other = resolve_target_policy(cfg, "p", "small")
    assert other.threshold_ratio == 0.7


def test_budget_formulas() -> None:
    policy = ResolvedTargetPolicy("p", "m", 0.8, 0.16, 1000)
    spec = resolve_compact_spec(policy, 10_000, reserved_completion_tokens=2000)
    # message_budget=8000, pressure=7000, threshold=min(8000,7000)=7000
    # retain=floor(8000*0.16)=1280
    assert spec.threshold_tokens == 7000
    assert spec.retain_tokens == 1280


def test_validation_errors() -> None:
    with pytest.raises(ValueError):
        resolve_target_policy({"thresholdRatio": 0}, "p", "m")
    with pytest.raises(ValueError):
        resolve_target_policy({"retainRatio": 1.5}, "p", "m")
    with pytest.raises(ValueError):
        resolve_target_policy({"retainRatio": 0.9}, "p", "m")
    with pytest.raises(ValueError):
        resolve_target_policy({"headroomTokens": -1}, "p", "m")
    with pytest.raises(ValueError):
        resolve_target_policy({"thresholdRatio": "bad"}, "p", "m")
    with pytest.raises(ValueError):
        resolve_compact_spec(ResolvedTargetPolicy("p", "m", 0.8, 0.16, 1000), 0)
    with pytest.raises(ValueError):
        resolve_compact_spec(
            ResolvedTargetPolicy("p", "m", 0.8, 0.16, 1000), 10_000, -5
        )
    with pytest.raises(ValueError):
        resolve_compact_spec(
            ResolvedTargetPolicy("p", "m", 0.8, 0.16, 1000), 10_000, 10_000
        )
    with pytest.raises(ValueError):
        resolve_compact_spec(
            ResolvedTargetPolicy("p", "m", 0.8, 0.16, 1000), 500
        )  # pressure budget <= 0
    with pytest.raises(ValueError):
        CompactSpec(threshold_tokens=0, retain_tokens=1)
    with pytest.raises(ValueError):
        CompactSpec(threshold_tokens=10, retain_tokens=10)


def test_non_mapping_override_ignored() -> None:
    policy = resolve_target_policy({"modelPolicies": ["garbage"]}, "p", "m")
    assert policy.threshold_ratio == 0.8
