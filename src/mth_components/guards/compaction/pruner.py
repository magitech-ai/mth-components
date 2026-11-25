"""Tool-result middle pruner with the reference byte-exact marker.

Ports packages/compaction/compaction-tool-result-pruner: collapse the middle
of an oversized tool result into a fixed marker, keeping head + tail code
points. Non-text blocks are preserved; text is measured in Unicode code
points, measured in Unicode code points.
"""

from __future__ import annotations

from typing import Any

PRUNE_MARKER = "\n\n[... tool result middle pruned ...]\n\n"
DEFAULT_THRESHOLD_CHARS = 8192
DEFAULT_HEAD_CHARS = 4096
DEFAULT_TAIL_CHARS = 1024


def prune_content(
    content: Any,
    *,
    threshold_chars: int = DEFAULT_THRESHOLD_CHARS,
    head_chars: int = DEFAULT_HEAD_CHARS,
    tail_chars: int = DEFAULT_TAIL_CHARS,
) -> list[Any] | None:
    """Return pruned content blocks, or None if nothing exceeds the budget."""
    if not isinstance(content, list):
        return None
    total = sum(
        len(block["text"])
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )
    if total <= threshold_chars:
        return None

    removed_start = head_chars
    removed_end = total - tail_chars
    result: list[Any] = []
    inserted = False
    block_start = 0
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            points = block["text"]
            block_end = block_start + len(points)
            head_end = min(len(points), max(0, removed_start - block_start))
            tail_start = min(len(points), max(0, removed_end - block_start))
            intersects = block_start < removed_end and block_end > removed_start
            if intersects and not inserted:
                result.append(
                    {
                        **block,
                        "text": points[:head_end] + PRUNE_MARKER + points[tail_start:],
                    }
                )
                inserted = True
            elif not intersects:  # pragma: no branch
                result.append(block)
            block_start = block_end
        else:
            result.append(block)
    if not inserted:  # pragma: no cover - no intersecting text block
        return None
    return result


__all__ = [
    "DEFAULT_HEAD_CHARS",
    "DEFAULT_TAIL_CHARS",
    "DEFAULT_THRESHOLD_CHARS",
    "PRUNE_MARKER",
    "prune_content",
]
