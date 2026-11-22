"""Compute a compact trust/status summary for an OKF v0.3 bundle.

Reads frontmatter directly (no full bundle traversal of bodies) so the
``okf_bundle`` tool can return trust signals alongside index.md without a
second tool call.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
MAX_SUMMARY_CONCEPTS = 40


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Return the YAML frontmatter mapping, or an empty dict when absent."""
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _as_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def trust_tier(frontmatter: dict[str, Any]) -> str:
    """Derive the OKF trust tier from current verification events."""
    generated = frontmatter.get("generated")
    generated_at = (
        _as_timestamp(generated.get("at")) if isinstance(generated, dict) else None
    )
    events = frontmatter.get("verified") or []
    if isinstance(events, dict):
        events = [events]
    current = []
    for event in events:
        if not isinstance(event, dict):
            continue
        at = _as_timestamp(event.get("at"))
        if at is None:
            continue
        if generated_at is None or at >= generated_at:
            current.append(event)
    if not current:
        return "unverified"
    actors = [str(event.get("by", "")) for event in current]
    if any(actor.startswith("human:") for actor in actors):
        return "human-reviewed"
    return "machine-confirmed"


def is_stale(frontmatter: dict[str, Any], now: datetime | None = None) -> bool:
    stale_after = _as_timestamp(frontmatter.get("stale_after"))
    if stale_after is None:
        return False
    return (now or datetime.now(UTC)) >= stale_after


def summarize_bundle(bundle_path: Path) -> str:
    """Return a compact trust/status block for every concept in a bundle."""
    rows: list[tuple[str, str, str, str, str]] = []
    for path in sorted(bundle_path.rglob("*.md")):
        if path.name in ("index.md", "log.md") or any(
            part.startswith(".") for part in path.relative_to(bundle_path).parts
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        frontmatter = parse_frontmatter(text)
        if not frontmatter.get("type"):
            continue
        rows.append(
            (
                path.stem,
                str(frontmatter.get("type")),
                str(frontmatter.get("status", "stable")),
                trust_tier(frontmatter),
                "yes" if is_stale(frontmatter) else "no",
            )
        )

    if not rows:
        return (
            '<trust_summary count="0">No concepts with OKF frontmatter.</trust_summary>'
        )

    shown = rows[:MAX_SUMMARY_CONCEPTS]
    lines = [f'<trust_summary count="{len(rows)}">']
    lines.append("concept | type | status | trust | stale")
    lines.append("--- | --- | --- | --- | ---")
    lines.extend(" | ".join(row) for row in shown)
    if len(rows) > len(shown):
        lines.append(f"... and {len(rows) - len(shown)} more concepts")
    lines.append("</trust_summary>")
    return "\n".join(lines)
