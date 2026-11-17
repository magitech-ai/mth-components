"""Unit tests for layered ApprovalGuard evaluation and interception."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from anima import Context
from magitech_harness.approval.service import ApprovalRequest, ApprovalService
from magitech_harness.session.events import EventType
from magitech_harness.session.store import Session
from magitech_harness.tools.service import ToolCallData

from mth_plugins.guards.approval.guard import ApprovalGuard
from mth_plugins.guards.approval.policy import ApprovalPolicy
from mth_plugins.guards.approval.types import (
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalScope,
    PolicyRule,
)


@pytest.fixture
def ctx() -> Context:
    context = Context()
    ApprovalService(context)
    return context


def _req(name: str = "mutate", digest: str = "sha256:1") -> ApprovalRequest:
    return ApprovalRequest(toolName=name, arguments={}, digest=digest)


@pytest.mark.asyncio
async def test_guard_policy_precedence(ctx: Context, tmp_path: Path) -> None:
    ws_file = tmp_path / "workspace_policy.toml"
    global_file = tmp_path / "global_policy.toml"
    ApprovalPolicy([]).save_rule(
        ws_file, PolicyRule(tool_pattern="bash", outcome=ApprovalOutcome.ALLOW)
    )
    ApprovalPolicy([]).save_rule(
        global_file, PolicyRule(tool_pattern="bash", outcome=ApprovalOutcome.DENY)
    )
    guard = ApprovalGuard(
        ctx=ctx, workspace_policy_path=ws_file, global_policy_path=global_file
    )
    assert await guard.evaluate_request(_req("bash")) is True


@pytest.mark.asyncio
async def test_guard_global_fallback(ctx: Context, tmp_path: Path) -> None:
    global_file = tmp_path / "global_policy.toml"
    ApprovalPolicy([]).save_rule(
        global_file, PolicyRule(tool_pattern="write_file", outcome=ApprovalOutcome.DENY)
    )
    guard = ApprovalGuard(ctx=ctx, global_policy_path=global_file)
    assert await guard.evaluate_request(_req("write_file")) is False


@pytest.mark.asyncio
async def test_guard_presenter_handling(ctx: Context, tmp_path: Path) -> None:
    ws_file = tmp_path / "ws.toml"
    guard = ApprovalGuard(ctx=ctx, workspace_policy_path=ws_file)
    req = _req()

    # No presenter, no rule -> fails closed
    assert await guard.evaluate_request(req) is False

    # Presenter throws -> fails closed
    guard.set_presenter(AsyncMock(side_effect=RuntimeError("closed")))
    assert await guard.evaluate_request(req) is False

    # Invalid object -> fails closed
    guard.set_presenter(AsyncMock(return_value={"outcome": "allow"}))
    assert await guard.evaluate_request(req) is False

    # Stale digest -> fails closed
    guard.set_presenter(
        AsyncMock(
            return_value=ApprovalDecision(
                outcome=ApprovalOutcome.ALLOW,
                scope=ApprovalScope.ONCE,
                request_digest="sha256:different",
            )
        )
    )
    assert await guard.evaluate_request(req) is False

    # DENY -> False
    guard.set_presenter(
        AsyncMock(
            return_value=ApprovalDecision(
                outcome=ApprovalOutcome.DENY,
                scope=ApprovalScope.ONCE,
                request_digest="sha256:1",
            )
        )
    )
    assert await guard.evaluate_request(req) is False

    # ALLOW -> True. No persistent grant is cached (finding #11).
    guard.set_presenter(
        AsyncMock(
            return_value=ApprovalDecision(
                outcome=ApprovalOutcome.ALLOW,
                scope=ApprovalScope.SESSION,
                request_digest="sha256:1",
            )
        )
    )
    assert await guard.evaluate_request(req) is True
    assert not hasattr(guard, "_session_grants")


@pytest.mark.asyncio
async def test_project_grant_not_persisted_by_default(
    ctx: Context, tmp_path: Path
) -> None:
    ws_file = tmp_path / "ws.toml"
    guard = ApprovalGuard(ctx=ctx, workspace_policy_path=ws_file)
    guard.set_presenter(
        AsyncMock(
            return_value=ApprovalDecision(
                outcome=ApprovalOutcome.ALLOW,
                scope=ApprovalScope.PROJECT,
                request_digest="sha256:p",
            )
        )
    )
    assert await guard.evaluate_request(_req("custom_tool", "sha256:p")) is True
    assert not ws_file.is_file()  # no blanket ALLOW persisted by default


@pytest.mark.asyncio
async def test_project_grant_persisted_when_opted_in(
    ctx: Context, tmp_path: Path
) -> None:
    ws_file = tmp_path / "ws.toml"
    guard = ApprovalGuard(
        ctx=ctx, workspace_policy_path=ws_file, persist_project_grants=True
    )
    guard.set_presenter(
        AsyncMock(
            return_value=ApprovalDecision(
                outcome=ApprovalOutcome.ALLOW,
                scope=ApprovalScope.PROJECT,
                request_digest="sha256:p2",
            )
        )
    )
    assert await guard.evaluate_request(_req("custom_tool2", "sha256:p2")) is True
    assert ws_file.is_file()
    assert "custom_tool2" in ws_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_guard_tool_pre_execute_interception(ctx: Context) -> None:
    guard = ApprovalGuard(ctx=ctx, require_all_mutations=True)
    next_mock = AsyncMock(return_value="executed")

    call = ToolCallData(call_id="c1", name="mutate", arguments={})
    # With no answerer, the new approval service fails closed.

    session = Session.create("guard-pre")
    session.append(EventType.TURN_START, {"turn": 1})
    object.__setattr__(call, "agent", SimpleNamespace(session=session))
    with pytest.raises(PermissionError, match="Approval denied"):
        await guard.handle_tool_pre_execute(call, next_mock)
    next_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_guard_safe_read_bypasses_and_no_service_passthrough(
    ctx: Context,
) -> None:
    guard = ApprovalGuard(ctx=ctx, require_all_mutations=True)
    next_mock = AsyncMock(return_value="ok")
    read_call = ToolCallData(call_id="r", name="read_file", arguments={})
    assert await guard.handle_tool_pre_execute(read_call, next_mock) == "ok"

    empty_ctx = Context()  # no approval service
    guard2 = ApprovalGuard(ctx=empty_ctx, require_all_mutations=False)
    call = ToolCallData(call_id="c", name="bash", arguments={})
    assert await guard2.handle_tool_pre_execute(call, next_mock) == "ok"


@pytest.mark.asyncio
async def test_guard_getattr_agent_raises_and_allowed_passthrough(ctx: Context) -> None:
    class _Raising:
        name = "bash"

        def __getattr__(self, name: str) -> Any:
            raise ValueError("no agent")

    guard = ApprovalGuard(ctx=ctx, require_all_mutations=False)
    next_mock = AsyncMock(return_value="ok")
    with pytest.raises(PermissionError, match="no agent"):
        await guard.handle_tool_pre_execute(_Raising(), next_mock)  # type: ignore[arg-type]

    # With an agent and an allowing service, execution proceeds.

    session = Session.create("allow")
    session.append(EventType.TURN_START, {"turn": 1})
    call = ToolCallData(call_id="a", name="bash", arguments={})
    object.__setattr__(call, "agent", SimpleNamespace(session=session))

    async def _allow(_r: Any) -> str:
        return "allowed-once"

    ctx.approval.set_handler(_allow)
    assert await guard.handle_tool_pre_execute(call, next_mock) == "ok"
