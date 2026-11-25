"""Tests for approval package init exports."""

from mth_components.guards import approval


def test_init_exports() -> None:
    assert hasattr(approval, "ApprovalGuard")
    assert hasattr(approval, "ApprovalPolicy")
    assert hasattr(approval, "ApprovalDecision")
    assert hasattr(approval, "ApprovalOutcome")
    assert hasattr(approval, "ApprovalScope")
    assert hasattr(approval, "ApprovalReasonCode")
    assert hasattr(approval, "PolicyRule")
    assert hasattr(approval, "apply")
