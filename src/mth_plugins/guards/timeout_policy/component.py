"""Anima component entry point for the tool-call timeout_policy guardrail."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from anima import Context

from mth_plugins.guards.timeout_policy.policy import TimeoutPolicy

__all__ = ["apply"]


def apply(
    ctx: Context,
    **kwargs: Any,
) -> None:
    """Mount the timeout_policy guard onto the Anima context.

    Hooks ``tools/execute`` to wrap tool dispatches with per-tool deadline
    signals and map deadline expiry to a structured ``TOOL_TIMEOUT`` error.
    """
    policy = TimeoutPolicy(ctx)

    async def _on_execute(
        exec_view: dict[str, Any],
        next_fn: Callable[..., Any],
    ) -> dict[str, Any]:
        return cast(dict[str, Any], await policy.on_execute(exec_view, next_fn))

    ctx.on("tools/execute", _on_execute)


apply.inject = ["tools"]  # type: ignore[attr-defined]
