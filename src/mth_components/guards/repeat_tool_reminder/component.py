"""Animas component entry point for the repeat_tool_reminder guardrail."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from animas import Context

from mth_components.guards.repeat_tool_reminder.guard import RepeatToolReminder

__all__ = ["apply"]


def apply(
    ctx: Context,
    *,
    thresholds: list[int] | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    arguments_preview_chars: int = 500,
    **kwargs: Any,
) -> None:
    """Mount the repeat_tool_reminder guard onto the Animas context.

    Hooks ``tools/post-execute`` to detect consecutive identical calls and
    ``agent/pre-step`` to clear per-agent chains on new user prompts.
    """
    reminder = RepeatToolReminder(
        thresholds=thresholds,
        include=include,
        exclude=exclude,
        arguments_preview_chars=arguments_preview_chars,
    )

    async def _post_execute(
        exec_view: dict[str, Any],
        frozen_input: dict[str, Any],
        next_fn: Callable[..., Any],
    ) -> dict[str, Any]:
        result = cast(dict[str, Any], await next_fn(exec_view, frozen_input))

        agent = exec_view.get("agent")
        if agent is None:
            return result

        tool_name = exec_view.get("name")
        if not tool_name:
            return result

        count = reminder.record(agent, tool_name, exec_view.get("arguments", {}))
        if count is None:
            return result

        message = reminder.reminder_text(
            count, tool_name, exec_view.get("arguments", {})
        )

        contexts = list(result.get("additionalContexts") or [])
        contexts.append(
            {
                "kind": "repeat_tool_reminder",
                "form": "notice",
                "summary": f"{tool_name} x {count}",
                "content": [{"type": "text", "text": message}],
            }
        )
        result["additionalContexts"] = contexts
        return result

    async def _pre_step(step_data: dict[str, Any], next_fn: Callable[..., Any]) -> Any:
        agent = step_data.get("agent")
        if agent is not None:
            reminder.reset_agent(agent)
        return cast(Any, await next_fn(step_data))

    ctx.on("tools/post-execute", _post_execute)
    ctx.on("agent/pre-step", _pre_step)


apply.inject = ["tools"]  # type: ignore[attr-defined]
