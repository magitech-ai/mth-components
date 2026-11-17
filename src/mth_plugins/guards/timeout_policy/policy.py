"""Tool-call timeout policy guard: enforces cooperative time limits on tool calls.

Reads ``timeoutMs`` from each tool's ``ToolDefinition`` and wraps the call's
``exec.signal`` with a deadline. When the deadline fires and downstream
cancellation settles, the result is replaced with a structured
``TOOL_TIMEOUT`` error. Tools without a declared limit are untouched.

The deadline is cooperative only — it cannot hard-stop a tool that ignores
cancellation.

Reference: ``packages/guard/timeout_policy`` in the parity monorepo.
"""

from __future__ import annotations

from typing import Any, cast

from anima import Context
from magitech_harness.agent.cancellation import AbortSignal, deadline, timeout_of

TOOL_TIMEOUT = "TOOL_TIMEOUT"

__all__ = [
    "TOOL_TIMEOUT",
    "TimeoutPolicy",
]


class TimeoutPolicy:
    """Wraps tool dispatches with per-tool deadline signals.

    Args:
        ctx: The Anima context (provides ``ctx.tools`` for tool definition
            lookup).
    """

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx

    def _timeout_ms(self, tool_name: str, agent: Any) -> float | None:
        """Read the declared ``timeoutMs`` from one tool's definition."""
        tool = self._ctx.tools.get(tool_name, agent)
        if tool is None:
            return None
        timeout_ms = tool.get("timeoutMs")
        if timeout_ms is None:
            return None
        return float(timeout_ms)

    async def on_execute(
        self,
        exec_view: dict[str, Any],
        next_fn: Any,
    ) -> dict[str, Any]:
        """Around-middleware listener for ``tools/execute``.

        Swaps ``exec_view["signal"]`` with a deadline signal for tools that
        declare ``timeoutMs``. After the downstream handler settles, replaces
        the result with a structured ``TOOL_TIMEOUT`` error if the deadline
        fired.
        """
        tool_name = exec_view["name"]
        agent = exec_view.get("agent")

        timeout_ms = self._timeout_ms(tool_name, agent)
        if timeout_ms is None:
            return cast(dict[str, Any], await next_fn(exec_view))

        original_signal: AbortSignal = exec_view["signal"]
        dl = deadline(original_signal, timeout_ms, TOOL_TIMEOUT)
        exec_view["signal"] = dl.signal

        try:
            result = await next_fn(exec_view)
        finally:
            exec_view["signal"] = original_signal
            dl.dispose()

        if timeout_of(dl.signal, TOOL_TIMEOUT) is not None:
            result = {
                "isError": True,
                "error": {
                    "name": "ToolTimeoutError",
                    "code": TOOL_TIMEOUT,
                    "reason": f"tool call timed out after {timeout_ms}ms",
                },
                "content": [
                    {
                        "type": "text",
                        "text": f"Error: tool call timed out after {timeout_ms}ms",
                    }
                ],
            }

        return cast(dict[str, Any], result)
