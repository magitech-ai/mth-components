"""Anima component entry point for output truncation guardrail."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from anima import Context

from mth_plugins.guards.truncation.truncation import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    format_size,
    truncate_head,
)

# Text content blocks carry a ``text`` field; other block types (image, file,
# tool-call, ...) are passed through unchanged.
_TEXT_BLOCK_TYPES = ("text", "reasoning")


def _truncate_content_blocks(
    content: Sequence[Any], max_lines: int, max_bytes: int
) -> tuple[list[Any], bool]:
    """Truncate text-bearing content blocks within the line/byte budget.

    Mirrors the legacy string path: once the budget is exhausted, the
    truncation notice is appended to the truncated block and every later
    block is dropped. Returns ``(new_blocks, changed)``; ``changed`` is False
    when nothing was altered.
    """
    new_blocks: list[Any] = []
    changed = False
    remaining_lines = max_lines
    remaining_bytes = max_bytes
    for block in content:
        if changed:
            # Budget exhausted: drop the remainder, like the legacy path.
            continue
        if (
            isinstance(block, Mapping)
            and block.get("type") in _TEXT_BLOCK_TYPES
            and isinstance(block.get("text"), str)
        ):
            trunc = truncate_head(
                block["text"], max_lines=remaining_lines, max_bytes=remaining_bytes
            )
            if trunc.truncated:
                changed = True
                shown = trunc.shown_end or 0
                total = trunc.total_lines
                notice = (
                    f"\n\n[Output truncated: showing {shown} of {total} lines "
                    f"({format_size(max_bytes)} limit reached).]"
                )
                new_blocks.append({**block, "text": trunc.text + notice})
            else:
                new_blocks.append(block)
                remaining_lines = max(0, remaining_lines - trunc.total_lines)
                remaining_bytes = max(0, remaining_bytes - trunc.total_bytes)
        else:
            new_blocks.append(block)
    return new_blocks, changed


def apply(ctx: Context, config: dict | None = None, **kwargs: Any) -> None:
    """Anima component applying post-execute tool truncation guardrail."""
    cfg = {**(config or {}), **kwargs}
    max_lines: int = cfg.get("max_lines", DEFAULT_MAX_LINES)
    max_bytes: int = cfg.get("max_bytes", DEFAULT_MAX_BYTES)

    async def _post_execute(
        output: Any, call_data: Any, next_fn: Callable[..., Any]
    ) -> Any:
        res = await next_fn(output, call_data)

        # Legacy Phase 2 path: ``tools/post-execute`` is called with
        # ``(raw_string_output, call_data)`` and expects a string back.
        if isinstance(output, str):
            if not isinstance(res, str):
                return res
            trunc = truncate_head(res, max_lines=max_lines, max_bytes=max_bytes)
            if not trunc.truncated:
                return res
            shown_lines = trunc.shown_end or 0
            return (
                f"{trunc.text}\n\n"
                f"[Output truncated: showing {shown_lines} of "
                f"{trunc.total_lines} lines ({format_size(max_bytes)} limit reached).]"
            )

        # New parity pipeline path: ``tools/post-execute`` is called with
        # ``(exec_view, result)`` where ``result`` is a ToolExecutionResult and
        # the handler must return a ``{"kind": "accept", ...}`` decision.
        result = call_data if isinstance(call_data, Mapping) else {}
        content = result.get("content")
        if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
            return res  # non-text result: pass through unchanged
        new_blocks, _ = _truncate_content_blocks(content, max_lines, max_bytes)
        return {"kind": "accept", "content": new_blocks}

    ctx.on("tools/post-execute", _post_execute)


apply.inject = ["tools"]  # type: ignore[attr-defined]
