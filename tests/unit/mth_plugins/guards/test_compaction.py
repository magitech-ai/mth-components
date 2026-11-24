"""Unit tests for context compaction guard component."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from animas import Context
from magitech_harness.compaction.service import CompactionService
from magitech_harness.llm.service import LLMService, MockLLMProvider
from magitech_harness.llm.types import ModelResponse
from magitech_harness.session.events import EventType, SessionEvent, ToolCallData
from magitech_harness.session.messages import (
    create_assistant_message,
    create_tool_result_message,
    create_user_message,
)
from magitech_harness.session.projection_registry import SessionProjectionRegistry
from magitech_harness.session.service import SessionService
from magitech_harness.session.store import Session

from mth_plugins.guards.compaction.compaction import (
    _safe_cut_by_tokens,
    compact_session,
    estimate_history_tokens,
    estimate_tokens,
    find_safe_cut_index,
    summarize_events,
)
from mth_plugins.guards.compaction.component import _resolve_spec, apply
from mth_plugins.guards.compaction.component import apply as compaction_apply
from mth_plugins.guards.compaction.config import CompactSpec
from mth_plugins.guards.compaction.pruner import PRUNE_MARKER, prune_content


def _make_event(
    ev_type: EventType | str,
    payload: dict[str, Any],
    session_id: str = "sess_test",
) -> SessionEvent:
    return SessionEvent(
        id=str(uuid.uuid4()),
        session_id=session_id,
        type=ev_type,
        payload=payload,
    )


def _append_user(ctx: Context, session: Session, text: str) -> None:
    """Appends one canonical user message to a session."""
    ctx.sessions.log_event(
        session,
        EventType.USER_MESSAGE,
        create_user_message(
            content=[{"type": "text", "text": text}],
            source={"kind": "user"},
        ),
    )


def _append_assistant(
    ctx: Context, session: Session, text: str, *, turn: int = 1, step: int = 1
) -> None:
    """Appends one canonical assistant message to a session."""
    ctx.sessions.log_event(
        session,
        EventType.ASSISTANT_MESSAGE,
        {
            "turn": turn,
            "step": step,
            "message": create_assistant_message(
                content=[{"type": "text", "text": text}],
                provider="mock",
                model="mock",
            ),
            "stream": [],
        },
    )


def _append_assistant_call(
    ctx: Context, session: Session, call_id: str, name: str
) -> None:
    """Appends one canonical assistant tool call with no result yet."""
    ctx.sessions.log_event(
        session,
        EventType.ASSISTANT_MESSAGE,
        {
            "turn": 1,
            "step": 1,
            "message": create_assistant_message(
                content=[
                    {
                        "type": "tool-call",
                        "id": call_id,
                        "name": name,
                        "arguments": "{}",
                    }
                ],
                provider="mock",
                model="mock",
            ),
            "stream": [],
        },
    )


def test_estimate_tokens() -> None:
    """Verifies token estimation heuristic."""
    assert estimate_tokens("") == 1
    assert estimate_tokens("hello world") >= 1
    long_text = "a" * 400
    assert estimate_tokens(long_text) == 100


def test_estimate_history_tokens() -> None:
    """Verifies token estimation across diverse event types."""
    events = [
        _make_event(
            EventType.USER_MESSAGE,
            {"text": "What is the capital of France?"},
        ),
        _make_event(
            EventType.ASSISTANT_MESSAGE,
            {
                "text": "Let me check.",
                "tool_calls": [
                    ToolCallData(
                        call_id="c1",
                        name="search",
                        arguments={"q": "capital of France"},
                    )
                ],
            },
        ),
        _make_event(
            EventType.TOOL_RESULT,
            {"call_id": "c1", "content": "Paris is the capital of France."},
        ),
        _make_event(
            EventType.COMPACTION_SUMMARY,
            {"summary": "User asked about Paris."},
        ),
        # The retired type is still read from old logs.
        _make_event(
            "session/compaction",
            {"summary": "Older summary."},
        ),
        _make_event(
            EventType.STEP_START,
            {"step_index": 1},
        ),
        # Canonical block shapes, as written by the current turn loop.
        _make_event(
            EventType.USER_MESSAGE,
            {
                "id": "u1",
                "role": "user",
                "content": [
                    {"type": "text", "text": "Canonical user"},
                    42,
                    {"type": "text", "text": 7},
                ],
                "source": {"kind": "user"},
            },
        ),
        _make_event(
            EventType.ASSISTANT_MESSAGE,
            {
                "turn": 1,
                "step": 1,
                "message": create_assistant_message(
                    content=[
                        {"type": "text", "text": "Canonical answer"},
                        {
                            "type": "tool-call",
                            "id": "c1",
                            "name": "search",
                            "arguments": "{}",
                        },
                        {"type": "tool-call", "name": "nameless"},
                    ],
                    provider="mock",
                    model="mock",
                ),
                "stream": [],
            },
        ),
        _make_event(
            EventType.TOOL_RESULT,
            {
                "turn": 1,
                "step": 1,
                "message": create_tool_result_message(
                    call_id="c1",
                    content=[{"type": "text", "text": "Canonical result"}],
                ),
            },
        ),
        _make_event(
            EventType.ASSISTANT_MESSAGE,
            {"message": {"content": "not-a-list"}},
        ),
        _make_event(EventType.TOOL_RESULT, {"output": "Output only result"}),
    ]
    total_tokens = estimate_history_tokens(events)
    assert total_tokens > 10


def test_find_safe_cut_index_empty_and_no_cut() -> None:
    """Verifies safe cut index on empty or insufficient events."""
    assert find_safe_cut_index([], 0) is None
    assert find_safe_cut_index([], 5) is None

    events = [
        _make_event(EventType.USER_MESSAGE, {"text": "hi"}),
    ]
    # Cut index 0 or negative
    assert find_safe_cut_index(events, 0) is None


def test_find_safe_cut_index_with_tool_pairing() -> None:
    """Verifies finding boundary that respects tool call/result pairing."""
    events = [
        _make_event(EventType.USER_MESSAGE, {"text": "msg 1"}),
        _make_event(
            EventType.ASSISTANT_MESSAGE,
            {
                "tool_calls": [ToolCallData(call_id="c1", name="bash", arguments={})],
            },
        ),
        _make_event(
            EventType.TOOL_RESULT,
            {"call_id": "c1", "content": "ok"},
        ),
        _make_event(
            EventType.ASSISTANT_MESSAGE,
            {
                "tool_calls": [ToolCallData(call_id="c2", name="bash", arguments={})],
            },
        ),
        # c2 tool result not in events yet
        _make_event(EventType.STEP_START, {"step_index": 2}),
    ]

    # Target cut index 4 includes c2 call without c2 result -> unsafe!
    # Should back up to index 3 (which includes c1 call and c1 result -> safe!)
    cut = find_safe_cut_index(events, 4)
    assert cut == 3

    # If completely unbalanced
    unbalanced = [
        _make_event(
            EventType.ASSISTANT_MESSAGE,
            {
                "tool_calls": [ToolCallData(call_id="c1", name="bash", arguments={})],
            },
        ),
        _make_event(EventType.STEP_START, {"step_index": 1}),
    ]
    assert find_safe_cut_index(unbalanced, 1) is None


@pytest.mark.asyncio
async def test_summarize_events_offline() -> None:
    """Verifies deterministic summarization when LLM is unavailable."""
    events = [
        _make_event(EventType.USER_MESSAGE, {"text": "Write a report"}),
        _make_event(
            EventType.ASSISTANT_MESSAGE,
            {
                "text": "Running analysis",
                "tool_calls": [
                    ToolCallData(call_id="c1", name="analyze", arguments={})
                ],
            },
        ),
        _make_event(
            EventType.TOOL_RESULT,
            {"call_id": "c1", "content": "Report data"},
        ),
        _make_event(
            EventType.COMPACTION_SUMMARY,
            {"summary": "Previous summary"},
        ),
        # The retired type still formats from old logs.
        _make_event(
            "session/compaction",
            {"summary": "Older summary"},
        ),
    ]
    summary = await summarize_events(events)
    assert "User: Write a report" in summary
    assert "Assistant: Running analysis" in summary
    assert "Tool call 'analyze'" in summary
    assert "Prior Summary: Previous summary" in summary
    assert "Prior Summary: Older summary" in summary


@pytest.mark.asyncio
async def test_summarize_events_canonical_shapes() -> None:
    """Verifies offline summarization reads canonical block messages."""
    events = [
        _make_event(
            EventType.USER_MESSAGE,
            create_user_message(
                content=[{"type": "text", "text": "Canonical user"}],
                source={"kind": "user"},
            ),
        ),
        _make_event(
            EventType.ASSISTANT_MESSAGE,
            {
                "turn": 1,
                "step": 1,
                "message": create_assistant_message(
                    content=[
                        {"type": "text", "text": "Canonical answer"},
                        {
                            "type": "tool-call",
                            "id": "c1",
                            "name": "search",
                            "arguments": "{}",
                        },
                    ],
                    provider="mock",
                    model="mock",
                ),
                "stream": [],
            },
        ),
        _make_event(
            EventType.TOOL_RESULT,
            {
                "turn": 1,
                "step": 1,
                "message": create_tool_result_message(
                    call_id="c1",
                    content=[{"type": "text", "text": "Canonical result"}],
                ),
            },
        ),
    ]
    summary = await summarize_events(events)
    assert "User: Canonical user" in summary
    assert "Assistant: Canonical answer" in summary
    assert "Tool call 'search'" in summary
    assert "Tool result: Canonical result" in summary


@pytest.mark.asyncio
async def test_summarize_events_with_llm() -> None:
    """Verifies LLM-based summarization when LLM service is available."""
    ctx = Context()
    ctx.use(LLMService)
    await ctx.flush()

    mock_provider = MockLLMProvider(
        [ModelResponse(text="Concise executive summary of conversation.")]
    )
    ctx.llm.register_provider("mock", mock_provider)

    events = [
        _make_event(EventType.USER_MESSAGE, {"text": "Help me build an agent"}),
    ]
    summary = await summarize_events(events, ctx=ctx)
    assert summary == "Concise executive summary of conversation."


@pytest.mark.asyncio
async def test_summarize_events_with_llm_failure_fallback() -> None:
    """Verifies graceful fallback to offline summary if LLM call fails."""
    ctx = Context()
    ctx.use(LLMService)
    await ctx.flush()

    class FailingProvider:
        async def generate(self, *args, **kwargs):
            raise RuntimeError("API timeout")

    ctx.llm.register_provider("failing", FailingProvider())

    events = [
        _make_event(EventType.USER_MESSAGE, {"text": "Fallback test"}),
    ]
    summary = await summarize_events(events, ctx=ctx)
    assert "User: Fallback test" in summary


@pytest.mark.asyncio
async def test_compact_session_success() -> None:
    """Verifies compacting session creates a compaction/summary event."""
    ctx = Context()
    ctx.use(SessionProjectionRegistry)
    ctx.use(SessionService)
    await ctx.flush()

    session = ctx.sessions.create("sess_compaction_1")
    _append_user(ctx, session, "Step 1")
    _append_assistant(ctx, session, "Step 1 answer")
    _append_user(ctx, session, "Step 2")
    _append_assistant(ctx, session, "Step 2 answer")

    res = await compact_session(ctx, session, target_ratio=0.5)
    assert res["compacted"] is True
    assert res["compacted_count"] == 2
    assert "summary" in res

    # The log-only summary is followed by the operative surface replace.
    assert any(e.type == EventType.COMPACTION_SUMMARY for e in session.events)
    checkpoint = session.events[-1]
    assert checkpoint.type == EventType.USER_MESSAGE
    assert (checkpoint.surface_op or {}).get("op") == "replace"

    # Compaction actually changes the derived transcript now (finding #15):
    # shadowed messages collapse into one checkpoint; the tail survives.
    msgs = session.derive_messages()
    assert any("<compacted-summary>" in json.dumps(m.get("content", [])) for m in msgs)
    assert {"type": "text", "text": "Step 2 answer"} in (msgs[-1].get("content") or [])


@pytest.mark.asyncio
async def test_compact_session_not_compacted() -> None:
    """Verifies compact_session returns compacted=False when history is too short."""
    ctx = Context()
    ctx.use(SessionProjectionRegistry)
    ctx.use(SessionService)
    await ctx.flush()

    session = ctx.sessions.create("sess_short")
    _append_user(ctx, session, "Single msg")

    res = await compact_session(ctx, session)
    assert res["compacted"] is False
    assert "reason" in res


@pytest.mark.asyncio
async def test_component_waterfall_session_compact() -> None:
    """Verifies session/compact waterfall trigger."""
    ctx = Context()
    ctx.use(SessionProjectionRegistry)
    ctx.use(SessionService)
    await ctx.flush()

    apply(ctx)

    session = ctx.sessions.create("sess_manual_compact")
    _append_user(ctx, session, "Turn 1")
    _append_assistant(ctx, session, "Answer 1")
    _append_user(ctx, session, "Turn 2")

    res = await ctx.waterfall(
        "session/compact",
        {"session": session, "status": "requested"},
        default_handler=lambda p: p,
    )

    assert res["compacted"] is True
    assert "summary" in res

    # Test waterfall without session
    res_no_sess = await ctx.waterfall(
        "session/compact",
        {"status": "requested"},
        default_handler=lambda p: p,
    )
    assert res_no_sess["compacted"] is False
    assert res_no_sess["error"] == "No session provided"


@pytest.mark.asyncio
async def test_component_waterfall_agent_pre_step_pressure() -> None:
    """Verifies agent/pre-step waterfall triggers compaction on token pressure."""
    ctx = Context()
    ctx.use(SessionProjectionRegistry)
    ctx.use(SessionService)
    await ctx.flush()

    # Configure very low max_tokens (e.g. 5 tokens) to trigger compaction
    apply(ctx, max_tokens=5, target_ratio=0.5)

    session = ctx.sessions.create("sess_pressure")
    _append_user(ctx, session, "Long user message with words")
    _append_assistant(ctx, session, "Long assistant response")
    _append_user(ctx, session, "Next question")

    # Trigger pre-step
    res = await ctx.waterfall(
        "agent/pre-step",
        {"action": "continue", "step_index": 1, "session": session},
        default_handler=lambda p: p,
    )

    assert res["action"] == "continue"
    assert any(e.type == EventType.COMPACTION_SUMMARY for e in session.events)


@pytest.mark.asyncio
async def test_summarize_events_edge_cases() -> None:
    """Verifies edge cases in formatting bullets and empty LLM response."""
    # Assistant message with only tool calls (no text)
    ev_tool_only = _make_event(
        EventType.ASSISTANT_MESSAGE,
        {
            "text": "",
            "tool_calls": [ToolCallData(call_id="c1", name="grep", arguments={})],
        },
    )
    # Assistant message with empty payload
    ev_empty_asst = _make_event(EventType.ASSISTANT_MESSAGE, {})
    # Ignored event type
    ev_turn = _make_event(EventType.TURN_START, {})

    summary = await summarize_events([ev_tool_only, ev_empty_asst, ev_turn])
    assert "Tool call 'grep'" in summary

    # LLM returning whitespace-only text
    ctx = Context()
    ctx.use(LLMService)
    await ctx.flush()
    ctx.llm.register_provider("mock", MockLLMProvider([ModelResponse(text="   ")]))
    summary_empty_llm = await summarize_events([ev_tool_only], ctx=ctx)
    assert "[Prior Context Summary]" in summary_empty_llm


@pytest.mark.asyncio
async def test_compact_session_unbalanced_boundary() -> None:
    """Verifies compact_session when no safe boundary can be found."""
    ctx = Context()
    ctx.use(SessionProjectionRegistry)
    ctx.use(SessionService)
    await ctx.flush()

    session = ctx.sessions.create("sess_unbalanced")
    # 2 events: first has unresolved tool call, second is turn start.
    _append_assistant_call(ctx, session, "c_unres", "tool")
    ctx.sessions.log_event(session, EventType.STEP_START, {"step_index": 1})

    res = await compact_session(ctx, session, target_ratio=0.5)
    assert res["compacted"] is False
    assert "No balanced boundary" in res["reason"]


@pytest.mark.asyncio
async def test_component_waterfall_agent_pre_step_below_threshold() -> None:
    """Verifies pre-step does not compact when token estimate is below max."""
    ctx = Context()
    ctx.use(SessionProjectionRegistry)
    ctx.use(SessionService)
    await ctx.flush()

    apply(ctx, max_tokens=10000)

    session = ctx.sessions.create("sess_small")
    _append_user(ctx, session, "Short message")

    res = await ctx.waterfall(
        "agent/pre-step",
        {"action": "continue", "step_index": 1, "session": session},
        default_handler=lambda p: p,
    )
    assert res["action"] == "continue"
    assert session.events[-1].type == EventType.USER_MESSAGE


@pytest.mark.asyncio
async def test_component_waterfall_agent_pre_step_no_session() -> None:
    """Verifies agent/pre-step behaves normally when session is not in payload."""
    ctx = Context()
    apply(ctx)

    res = await ctx.waterfall(
        "agent/pre-step",
        {"action": "continue", "step_index": 1},
        default_handler=lambda p: p,
    )
    assert res["action"] == "continue"


def test_tool_result_pruner_marker_bytes() -> None:
    """The pruner emits the reference-exact marker and keeps head+tail."""
    assert PRUNE_MARKER == "\n\n[... tool result middle pruned ...]\n\n"
    assert len(PRUNE_MARKER.encode("utf-8")) == 39
    long_text = "X" * 9000
    result = prune_content([{"type": "text", "text": long_text}])
    assert result is not None
    assert PRUNE_MARKER in result[0]["text"]
    assert result[0]["text"].count("X") == 4096 + 1024


def test_tool_result_pruner_noop_when_small() -> None:
    assert prune_content([{"type": "text", "text": "short"}]) is None


def test_pruner_preserves_non_text_blocks_and_empty() -> None:
    result = prune_content(
        [
            {"type": "image", "data": "x"},
            {"type": "text", "text": "A" * 9000},
            {"type": "text", "text": "B" * 200},
        ]
    )
    assert result is not None
    assert result[0] == {"type": "image", "data": "x"}
    assert PRUNE_MARKER in result[1]["text"]
    assert result[2] == {"type": "text", "text": "B" * 200}
    assert prune_content("not a list") is None


def test_pruner_without_intersecting_text_block() -> None:
    # Only non-text blocks -> nothing to prune.
    assert prune_content([{"type": "image", "data": "x"}]) is None


@pytest.mark.asyncio
async def test_component_registers_pruner_and_prunes_session() -> None:

    ctx = Context()
    ctx.use(SessionProjectionRegistry)
    ctx.use(SessionService)
    ctx.use(CompactionService)
    await ctx.flush()
    compaction_apply(ctx)
    assert "tool-result" in ctx.compaction.list_pruners()

    # Exercise the registered pruner on a real Session with an oversized
    # surface tool/result. The fix appends a compaction/prune + replace event
    # rather than mutating a detached payload copy.
    session = ctx.sessions.create("pruner-session")
    ctx.sessions.log_event(session, "turn/start", {"turn": 1})
    ctx.sessions.log_event(session, "step/start", {"turn": 1, "step": 1})
    # A non-tool surface node and a small (unprunable) tool result exercise the
    # pruner's skip branches.
    ctx.sessions.log_event(
        session,
        "user/message",
        {"content": [{"type": "text", "text": "hi"}], "source": {"kind": "user"}},
        surface_op="append",
    )
    ctx.sessions.log_event(
        session,
        "tool/result",
        {
            "turn": 1,
            "step": 1,
            "message": create_tool_result_message(
                call_id="small",
                content=[{"type": "text", "text": "tiny"}],
            ),
        },
        surface_op="append",
    )
    ctx.sessions.log_event(
        session,
        "tool/result",
        {
            "turn": 1,
            "step": 1,
            "message": create_tool_result_message(
                call_id="c1",
                content=[{"type": "text", "text": "Q" * 9000}],
            ),
        },
        surface_op="append",
    )
    pruner = ctx.compaction._pruners["tool-result"]
    out = pruner(session)
    assert out["pruned"] is True and out["count"] == 1
    assert any(e.type == EventType.COMPACTION_PRUNE for e in session.events)
    replacements = [
        e
        for e in session.events
        if e.type == EventType.TOOL_RESULT
        and isinstance(e.surface_op, dict)
        and e.surface_op.get("op") == "replace"
    ]
    assert len(replacements) == 1
    assert replacements[0].source_event_seqs is not None
    assert "[... tool result middle pruned ...]" in json.dumps(
        replacements[0].payload.get("message", {})
    )


@pytest.mark.asyncio
async def test_compact_session_with_token_spec() -> None:
    """A CompactSpec drives the cut by token budget, not event count."""
    ctx = Context()
    ctx.use(SessionProjectionRegistry)
    ctx.use(SessionService)
    await ctx.flush()
    session = ctx.sessions.create("budget-compact")
    for text in ("First question", "First answer", "Second question", "Second answer"):
        if "question" in text:
            _append_user(ctx, session, text)
        else:
            _append_assistant(ctx, session, text)

    spec = CompactSpec(threshold_tokens=100, retain_tokens=5)
    res = await compact_session(ctx, session, spec=spec)
    assert res["compacted"] is True
    assert "summary" in res
    assert any(e.type == EventType.COMPACTION_SUMMARY for e in session.events)
    checkpoint = session.events[-1]
    assert checkpoint.type == EventType.USER_MESSAGE
    assert isinstance(checkpoint.surface_op, dict)
    assert checkpoint.surface_op.get("op") == "replace"


@pytest.mark.asyncio
async def test_resolve_spec_paths() -> None:
    """_resolve_spec handles durable context, header, adapter, and fallback paths."""

    class FakeCtx:
        def __init__(self, llm=None):
            self._llm = llm

        def has(self, name):
            return name == "llm" and self._llm is not None

        @property
        def llm(self):
            return self._llm

    class FakeSession:
        def __init__(self, rc=None, rh=None):
            self._rc = rc
            self._rh = rh

        def request_context(self):
            return self._rc

        def request_header(self):
            return self._rh

    # Durable context supplies the window; header supplies reserved tokens.
    spec = await _resolve_spec(
        FakeCtx(),
        FakeSession(
            rc={"provider": "p", "model": "m", "contextWindow": 200000},
            rh={"config": {"maxTokens": 2000}},
        ),
        {},
    )
    assert spec is not None
    # message_budget=198000, pressure=132464, threshold=min(160000,132464)=132464
    # retain=floor(198000*0.16)=31680
    assert spec.threshold_tokens == 132464
    assert spec.retain_tokens == 31680

    # Adapter-owned context + defaultMaxTokens is used when no header reserved.
    class FakeLlm:
        async def resolve_model_info(self, provider, model):
            return {
                "context": {"contextWindow": 200000},
                "defaultMaxTokens": 2000,
            }

    spec = await _resolve_spec(
        FakeCtx(FakeLlm()),
        FakeSession(rc={"provider": "p", "model": "m"}),
        {},
    )
    assert spec is not None and spec.threshold_tokens == 132464

    # Adapter raising falls through to the configured/fallback window.
    class FailingLlm:
        async def resolve_model_info(self, provider, model):
            raise RuntimeError("no adapter")

    spec = await _resolve_spec(
        FakeCtx(FailingLlm()),
        FakeSession(rc={"provider": "p", "model": "m"}),
        {},
    )
    assert spec is not None

    # Non-positive context window returns None (fallback disabled by config).
    spec = await _resolve_spec(FakeCtx(), FakeSession(), {"context_window": 0})
    assert spec is None

    # Durable context without contextWindow uses the fallback constant.
    spec = await _resolve_spec(
        FakeCtx(), FakeSession(rc={"provider": "p", "model": "m"}), {}
    )
    assert spec is not None


def test_safe_cut_by_tokens_empty() -> None:
    assert _safe_cut_by_tokens([], 10) is None


@pytest.mark.asyncio
async def test_resolve_spec_reserved_and_error_fallbacks() -> None:
    """Remaining _resolve_spec branches: pre-set window, non-dict header, spec error."""

    class FakeCtx:
        def has(self, name):
            return False

    class FakeSession:
        def __init__(self, rc, rh):
            self._rc, self._rh = rc, rh

        def request_context(self):
            return self._rc

        def request_header(self):
            return self._rh

    # Configured window skips durable lookup; non-dict header skips reserved.
    spec = await _resolve_spec(
        FakeCtx(),
        FakeSession(rc={"provider": "p", "model": "m"}, rh="not-a-dict"),
        {"context_window": 200000},
    )
    assert spec is not None

    # A tiny window whose pressure budget is invalid makes resolve_compact_spec raise.
    spec = await _resolve_spec(
        FakeCtx(), FakeSession(rc=None, rh=None), {"context_window": 100}
    )
    assert spec is None


@pytest.mark.asyncio
async def test_resolve_spec_header_config_shapes() -> None:
    """Cover non-dict header config and adapter default branch decisions."""

    class FakeCtx:
        def has(self, name):
            return False

    class FakeSession:
        def request_context(self):
            return {"provider": "p", "model": "m"}

        def request_header(self):
            return {"config": "not-a-dict"}

    spec = await _resolve_spec(FakeCtx(), FakeSession(), {"context_window": 200000})
    assert spec is not None


@pytest.mark.asyncio
async def test_resolve_spec_reserved_from_header_skips_adapter_default() -> None:
    """When the header reserves tokens, the adapter defaultMaxTokens is not used."""

    class FakeCtx:
        def has(self, name):
            return name == "llm"

        @property
        def llm(self):
            class L:
                async def resolve_model_info(self, provider, model):
                    return {"context": {}, "defaultMaxTokens": 9999}

            return L()

    class FakeSession:
        def request_context(self):
            return {"provider": "p", "model": "m"}

        def request_header(self):
            return {"config": {"maxTokens": 2000}}

    spec = await _resolve_spec(FakeCtx(), FakeSession(), {"context_window": 200000})
    assert spec is not None
    # reserved=2000 from header (adapter's 9999 is ignored because reserved != 0);
    # pressure = (200000-2000) - 65536 = 132464
    assert spec.threshold_tokens == 132464
