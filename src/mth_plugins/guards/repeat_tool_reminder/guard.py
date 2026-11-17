"""Repeat-tool reminder guard: detects consecutive identical tool calls per agent.

Tracks repeat chains keyed by (tool name, canonical arguments). When a
configured threshold is reached, delivers an advisory reminder through the
post-execute decision's ``additionalContexts``. The reminder is advisory only
— it never blocks or delays a call.

Reference: ``packages/guard/repeat_tool_reminder`` in the parity monorepo.
"""

from __future__ import annotations

import json
from typing import Any


def canonical_arguments(arguments: Any) -> str:
    """Canonicalize tool arguments for repeat-chain identity.

    Deep key-sort plus deterministic JSON serialization means property order
    is ignored and two calls with the same arguments (regardless of key
    order) produce the same signature.
    """
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"))


class RepeatToolReminder:
    """Tracks per-agent repeat chains and emits reminders at thresholds.

    Args:
        thresholds: Repeat counts that trigger a reminder (e.g. [3, 5, 8]).
        include: Only these tool names are tracked; empty means all.
        exclude: These tool names are never tracked.
        arguments_preview_chars: Cap on characters of arguments shown in a
            detailed reminder.
    """

    def __init__(
        self,
        *,
        thresholds: list[int] | None = None,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        arguments_preview_chars: int = 500,
    ) -> None:
        if thresholds is None:
            thresholds = [3, 5, 8]
        if not thresholds:
            raise ValueError("repeat_tool_reminder thresholds must not be empty")
        if any(t < 2 for t in thresholds):
            raise ValueError("repeat_tool_reminder thresholds must be >= 2")
        if len(thresholds) != len(set(thresholds)):
            raise ValueError("repeat_tool_reminder thresholds must be unique")
        self._thresholds = sorted(thresholds)
        self._include = set(include) if include else None
        self._exclude = set(exclude) if exclude else None
        self._arguments_preview_chars = arguments_preview_chars
        # agent_id -> {signature: count}
        self._chains: dict[Any, dict[str, int]] = {}

    def _is_tracked(self, tool_name: str) -> bool:
        if self._include is not None and tool_name not in self._include:
            return False
        return not (self._exclude is not None and tool_name in self._exclude)

    def record(
        self,
        agent: Any,
        tool_name: str,
        arguments: Any,
    ) -> int | None:
        """Record one call and return the new count if a threshold fired.

        Returns the new count when it matches a configured threshold,
        otherwise ``None``.
        """
        if not self._is_tracked(tool_name):
            return None

        signature = f"{tool_name}:{canonical_arguments(arguments)}"
        chain = self._chains.get(agent)
        if chain is None:
            chain = {}
            self._chains[agent] = chain

        count = chain.get(signature, 0) + 1
        chain[signature] = count

        if count in self._thresholds:
            return count
        return None

    def reset_agent(self, agent: Any) -> None:
        """Clear the repeat chain for one agent (called on new user prompt)."""
        self._chains.pop(agent, None)

    def chains(self) -> dict[Any, dict[str, int]]:
        """Return a shallow copy of all chains (for testing/introspection)."""
        return {agent: dict(chain) for agent, chain in self._chains.items()}

    def reminder_text(
        self,
        count: int,
        tool_name: str,
        arguments: Any,
    ) -> str:
        """Build the reminder message for one threshold firing."""
        if count == self._thresholds[0]:
            return (
                "You are repeating the exact same tool call with identical "
                "arguments. Carefully analyze the previous result before "
                "calling again: if the task is not complete, try a different "
                "approach or different arguments instead of repeating the call."
            )

        preview = canonical_arguments(arguments)
        if len(preview) > self._arguments_preview_chars:
            preview = preview[: self._arguments_preview_chars] + (
                f" … (+{len(preview) - self._arguments_preview_chars} more chars)"
            )

        return (
            "Repeated tool call detected:\n"
            f"- tool: {tool_name}\n"
            f"- consecutive_calls: {count}\n"
            f"- arguments: {preview}\n"
            "The repeated calls are not making progress. Do not call this tool "
            "with these exact arguments again. Inspect the latest result and "
            "choose a different action, different arguments, or finish the "
            "task if enough evidence has been gathered."
        )
