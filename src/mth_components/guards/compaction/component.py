"""Animas component entry point for context compaction guardrail."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from animas import Context
from animas.events import EventMode

from mth_components.guards.compaction.compaction import (
    compact_session,
    estimate_history_tokens,
)
from mth_components.guards.compaction.config import (
    FALLBACK_CONTEXT_WINDOW,
    CompactSpec,
    resolve_compact_spec,
    resolve_target_policy,
)


async def _resolve_spec(
    ctx: Context, session: Any, config: dict[str, Any]
) -> CompactSpec | None:
    """Resolve a token-budget CompactSpec from the session's durable target.

    Prefer the adapter-owned contextWindow (reference: resolveModelInfo), then
    the durable request/context event, then a configured/fallback constant.
    Returns None when no usable config is available so callers fall back to the
    legacy heuristic.
    """
    provider = model = None
    context_window = config.get("context_window", config.get("contextWindow"))
    request_context = getattr(session, "request_context", lambda: None)()
    if isinstance(request_context, dict):
        provider = request_context.get("provider")
        model = request_context.get("model")
        if context_window is None:
            context_window = request_context.get("contextWindow")

    # Reserved completion tokens: durable request header wins; the adapter's
    # defaultMaxTokens is the fallback (reference reservedCompletionTokens).
    reserved = 0
    request_header = getattr(session, "request_header", lambda: None)()
    if isinstance(request_header, dict):
        header_config = request_header.get("config")
        if isinstance(header_config, dict):
            reserved = int(header_config.get("maxTokens", 0) or 0)

    # Adapter-owned capacity is the authoritative source.
    if ctx.has("llm") and provider and model:
        try:
            info = await ctx.llm.resolve_model_info(provider, model)
            context = info.get("context") if isinstance(info, dict) else None
            if isinstance(context, dict) and context.get("contextWindow"):
                context_window = context["contextWindow"]
            if reserved == 0 and isinstance(info, dict):
                reserved = int(info.get("defaultMaxTokens", 0) or 0)
        except Exception:
            pass

    if context_window is None:
        context_window = FALLBACK_CONTEXT_WINDOW
    context_window = int(context_window)
    if context_window <= 0:
        return None

    policy = resolve_target_policy(config, provider or "", model or "")
    try:
        return resolve_compact_spec(policy, context_window, reserved)
    except ValueError:
        return None


def apply(ctx: Context, config: dict | None = None, **kwargs: Any) -> None:
    """Animas component registering compaction guardrail and session/compact handler."""
    cfg = {**(config or {}), **kwargs}
    max_tokens: int | None = cfg.get("max_tokens")
    legacy_ratio: float | None = cfg.get("target_ratio")
    use_budget = "threshold_ratio" in cfg or "retain_ratio" in cfg or max_tokens is None

    async def _spec(session: Any) -> CompactSpec | None:
        if not use_budget:
            return None
        return await _resolve_spec(ctx, session, cfg)

    async def _on_session_compact(
        payload: dict[str, Any], next_fn: Callable[..., Any]
    ) -> dict[str, Any]:
        session = payload.get("session")
        if session is None and ctx.has("sessions"):
            session = getattr(ctx.sessions, "active_session", None)
        if session is None:
            no_sess_res = await next_fn(
                {**payload, "compacted": False, "error": "No session provided"}
            )
            return cast(dict[str, Any], no_sess_res)

        res = await compact_session(
            ctx, session, spec=await _spec(session), target_ratio=legacy_ratio
        )
        compact_res = await next_fn({**payload, **res})
        return cast(dict[str, Any], compact_res)

    async def _on_pre_step(
        payload: dict[str, Any], next_fn: Callable[..., Any]
    ) -> dict[str, Any]:
        session = payload.get("session")
        if session is not None and ctx.has("sessions"):
            est_tokens = estimate_history_tokens(session.events)
            spec = await _spec(session)
            threshold = (
                spec.threshold_tokens if spec is not None else (max_tokens or 8000)
            )
            # Model-free pruning runs before the budget check (reference behavior).
            if ctx.has("compaction"):
                await ctx.compaction.prune(session)
                est_tokens = estimate_history_tokens(session.events)
            if est_tokens >= threshold:
                await compact_session(
                    ctx, session, spec=spec, target_ratio=legacy_ratio
                )
        pre_step_res = await next_fn(payload)
        return cast(dict[str, Any], pre_step_res)

    # The tool-result pruner is a base-bundle row in magitech-harness and is
    # already registered on ``ctx.compaction`` by that bundle. Registering it
    # here as well would raise "pruner already registered"; this guard owns only
    # the conversation-level compaction policy below.

    ctx.on("session/compact", _on_session_compact, mode=EventMode.WATERFALL)
    ctx.on("agent/pre-step", _on_pre_step, mode=EventMode.WATERFALL)


apply.inject = ["sessions"]  # type: ignore[attr-defined]
