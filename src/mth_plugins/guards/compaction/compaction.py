"""Context compaction logic and history folding algorithms."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from anima import Context
from magitech_harness.session.events import EventType, SessionEvent
from magitech_harness.session.pairing import is_tool_pairing_balanced
from magitech_harness.session.store import Session
from magitech_harness.session.surface import fold_surface

logger = logging.getLogger("mth_plugins.guards.compaction")

_RETIRED_COMPACTION_TYPE = "session/compaction"
"""Retired summary event, read-only: old logs still carry it, and the
read side below still understands it. New summaries are written as
``compaction/summary``; the full compaction engine sequence is Phase 4."""


def _blocks_text(content: list[Any]) -> str:
    """Text of canonical content blocks, ignoring non-text blocks."""
    return "".join(
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )


def _content_text(payload: dict[str, Any]) -> str:
    """Text carried by one event payload, flat or canonical block shape."""
    for value in (payload.get("text"), payload.get("content"), payload.get("output")):
        if isinstance(value, str):
            return value
    content = payload.get("content")
    if isinstance(content, list):
        return _blocks_text(content)
    message = payload.get("message")
    if isinstance(message, dict):
        nested = message.get("content")
        if isinstance(nested, list):
            return _blocks_text(nested)
    return ""


def _tool_call_names(payload: dict[str, Any]) -> list[str]:
    """Tool-call names on one assistant payload, flat or canonical shape."""
    names = [getattr(tc, "name", str(tc)) for tc in (payload.get("tool_calls") or [])]
    message = payload.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        names.extend(
            block["name"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "tool-call"
            and isinstance(block.get("name"), str)
        )
    return names


def estimate_tokens(text: str) -> int:
    """Estimates the number of tokens in a string using a 4-char heuristic."""
    if not text:
        return 1
    return max(1, len(text) // 4)


def estimate_history_tokens(events: list[SessionEvent]) -> int:
    """Estimates the cumulative token count of a sequence of session events."""
    total = 0
    for ev in events:
        if ev.type == EventType.USER_MESSAGE:
            total += estimate_tokens(_content_text(ev.payload))
        elif ev.type == EventType.ASSISTANT_MESSAGE:
            total += estimate_tokens(_content_text(ev.payload))
            for name in _tool_call_names(ev.payload):
                total += estimate_tokens(name)
        elif ev.type == EventType.TOOL_RESULT:
            total += estimate_tokens(_content_text(ev.payload))
        elif ev.type in (_RETIRED_COMPACTION_TYPE, EventType.COMPACTION_SUMMARY):
            total += estimate_tokens(str(ev.payload.get("summary", "")))
        else:
            total += 2
    return total


def find_safe_cut_index(
    events: list[SessionEvent], target_cut_index: int
) -> int | None:
    """Finds the largest safe cut boundary <= target_cut_index with balanced pairing."""
    if not events or target_cut_index <= 0:
        return None

    max_idx = min(target_cut_index, len(events) - 1)
    for cut in range(max_idx, 0, -1):
        if is_tool_pairing_balanced(events, cut - 1):
            return cut
    return None


def _format_event_bullet(ev: SessionEvent) -> str | None:
    """Formats a single session event into a readable summary bullet."""
    if ev.type == EventType.USER_MESSAGE:
        return f"- User: {_content_text(ev.payload)}"
    if ev.type == EventType.ASSISTANT_MESSAGE:
        text = _content_text(ev.payload)
        tc_names = _tool_call_names(ev.payload)
        items = []
        if text:
            items.append(f"Assistant: {text}")
        items.extend(f"Tool call '{name}'" for name in tc_names)
        return "- " + "; ".join(items) if items else None
    if ev.type == EventType.TOOL_RESULT:
        return f"- Tool result: {_content_text(ev.payload)}"
    if ev.type in (_RETIRED_COMPACTION_TYPE, EventType.COMPACTION_SUMMARY):
        return f"- Prior Summary: {ev.payload.get('summary', '')}"
    return None


async def summarize_events(
    events: list[SessionEvent], ctx: Context | None = None
) -> str:
    """Summarizes a slice of session events using LLM if available, else fallback."""
    bullets: list[str] = []
    for ev in events:
        bullet = _format_event_bullet(ev)
        if bullet:
            bullets.append(bullet)

    joined_bullets = "\n".join(bullets)

    if ctx is not None and ctx.has("llm") and ctx.llm.providers:
        prompt = (
            "Summarize the following conversation history concisely into a brief "
            f"context checkpoint:\n\n{joined_bullets}"
        )
        try:
            res = await ctx.llm.call(messages=[{"role": "user", "content": prompt}])
            if res.text.strip():
                return str(res.text).strip()
        except Exception as exc:
            logger.warning("LLM summarization failed (%s); using fallback", exc)

    return f"[Prior Context Summary]\n{joined_bullets}"


def _safe_cut_by_tokens(events: list[SessionEvent], retain_tokens: int) -> int | None:
    """Pick the event index whose tail keeps ~``retain_tokens`` verbatim.

    Walks surface-bearing events from the end accumulating token estimates,
    then hands the index to the pairing-safe boundary selector. This mirrors
    the token-budget retention tail.
    """
    if not events or retain_tokens <= 0:
        return None
    accumulated = 0
    cut = len(events)
    for index in range(len(events) - 1, -1, -1):
        accumulated += estimate_history_tokens([events[index]])
        if accumulated >= retain_tokens:
            cut = index
            break
    # Keep at least one event in the head and one in the tail.
    cut = max(1, min(cut, len(events) - 1))
    return find_safe_cut_index(events, cut)


async def compact_session(
    ctx: Context,
    session: Session,
    *,
    spec: Any | None = None,
    target_ratio: float | None = None,
    custom_summary: str | None = None,
) -> dict[str, Any]:
    """Compacts session history by folding prior events into a summary event.

    When ``spec`` is supplied its token budgets drive the cut (reference
    compaction-basic behavior). ``target_ratio`` is the legacy event-count
    heuristic, retained only for direct callers that have not resolved a spec.
    The summary is written as ``compaction/summary``: the retired
    ``session/compaction`` type can no longer be appended.
    """
    if len(session.events) < 2:
        return {
            "compacted": False,
            "reason": "Session history too short to compact",
        }

    if spec is not None:
        cut_idx = _safe_cut_by_tokens(session.events, spec.retain_tokens)
    else:
        ratio = 0.5 if target_ratio is None else target_ratio
        target_cut = max(1, int(len(session.events) * ratio))
        cut_idx = find_safe_cut_index(session.events, target_cut)

    if cut_idx is None or cut_idx <= 0:
        return {
            "compacted": False,
            "reason": "No balanced boundary found for compaction",
        }

    events_to_compact = session.events[:cut_idx]
    summary = custom_summary or await summarize_events(events_to_compact, ctx=ctx)
    summary_blocks = (
        [{"type": "text", "text": summary}] if isinstance(summary, str) else summary
    )
    # Compute the CURRENT surface nodes so the replace op references live
    # nodes (an earlier compaction may have already shadowed some events).
    surface_nodes = fold_surface(session.events).nodes
    compact_seqs = {ev.seq for ev in events_to_compact}
    # Never shadow the system/message head (reference selectCompactableRange):
    # it is the stable prompt-cache prefix and surface rules forbid rewriting it.
    shadowed_surface = [seq for seq in surface_nodes if seq in compact_seqs]
    if shadowed_surface and shadowed_surface[0] == surface_nodes[0]:
        head_event = session.events[surface_nodes[0]]
        if head_event.type == EventType.SYSTEM_MESSAGE:
            shadowed_surface = shadowed_surface[1:]
    if not shadowed_surface:  # pragma: no cover - no surface in range
        return {
            "compacted": False,
            "reason": "No surface nodes in the compactable range",
        }

    start_seq = shadowed_surface[0]
    end_seq = shadowed_surface[-1]
    shadowed_seqs = shadowed_surface
    shadowed_token_count = estimate_history_tokens(events_to_compact)
    compaction_id = f"cmp-{uuid.uuid4().hex[:8]}"

    # 1. Log-only compaction/summary (durable audit detail).
    summary_event = ctx.sessions.log_event(
        session,
        EventType.COMPACTION_SUMMARY,
        {
            "compactionId": compaction_id,
            "summary": summary_blocks,
            "shadowedRange": {"start": start_seq, "end": end_seq},
            "shadowedSeqs": shadowed_seqs,
            "shadowedTokenCount": shadowed_token_count,
            "provider": "compaction",
            "model": "summary",
            "compacted_count": cut_idx,
        },
    )

    # 2. The operative surface replace: a user/message checkpoint that folds
    #    the shadowed span (with framing markers) into derive_messages().
    checkpoint_content = [
        {
            "type": "text",
            "text": (
                "This is an automatically generated checkpoint condensing an "
                "earlier span of the conversation.\n\n<compacted-summary>\n"
            ),
        },
        *summary_blocks,
        {"type": "text", "text": "\n</compacted-summary>"},
    ]
    ctx.sessions.log_event(
        session,
        EventType.USER_MESSAGE,
        {
            "content": checkpoint_content,
            "role": "user",
            "id": f"checkpoint-{compaction_id}",
            "source": {"kind": "compaction", "compactionId": compaction_id},
        },
        surface_op={"op": "replace", "startSeq": start_seq, "endSeq": end_seq},
        source_event_seqs=[summary_event.seq, *shadowed_surface],
    )

    return {
        "compacted": True,
        "compacted_count": cut_idx,
        "summary": summary,
    }
