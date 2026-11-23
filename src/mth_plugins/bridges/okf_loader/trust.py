"""Compute a compact trust/status summary for an OKF v0.3 bundle.

Reads frontmatter directly (no full bundle traversal of bodies) so the
``okf_bundle`` tool can return trust signals alongside index.md without a
second tool call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
MAX_SUMMARY_CONCEPTS = 40
MAX_DESCRIPTION_CHARS = 160
MAX_BODY_CONCEPTS = 8
MAX_BODY_BYTES = 32_768


@dataclass(frozen=True)
class ConceptMeta:
    """Frontmatter-only routing data for one concept in a bundle."""

    path: str
    concept: str
    title: str
    description: str
    type: str
    status: str
    trust: str
    stale: bool


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


def collect_concepts(bundle_path: Path) -> list[ConceptMeta]:
    """Read frontmatter-only routing data for every concept in a bundle."""
    concepts: list[ConceptMeta] = []
    root = bundle_path.resolve()
    for path in sorted(bundle_path.rglob("*.md")):
        if path.name in ("index.md", "log.md") or any(
            part.startswith(".") for part in path.relative_to(bundle_path).parts
        ):
            continue
        if not path.resolve().is_relative_to(root):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        frontmatter = parse_frontmatter(text)
        if not frontmatter.get("type"):
            continue
        concepts.append(
            ConceptMeta(
                path=path.relative_to(bundle_path).as_posix(),
                concept=path.stem,
                title=str(frontmatter.get("title", "")),
                description=str(frontmatter.get("description", "")),
                type=str(frontmatter["type"]),
                status=str(frontmatter.get("status", "stable")),
                trust=trust_tier(frontmatter),
                stale=is_stale(frontmatter),
            )
        )
    return concepts


def load_concept_bodies(
    bundle_path: Path,
    ids: list[str],
    *,
    max_concepts: int = MAX_BODY_CONCEPTS,
    max_bytes: int = MAX_BODY_BYTES,
) -> tuple[str, list[str]]:
    """Render selected concept bodies with metadata in one bounded block.

    Returns ``(rendered, omitted)``. Omitted IDs include unknown concepts and
    anything dropped by the concept-count or byte budgets.
    """
    known = {concept.concept: concept for concept in collect_concepts(bundle_path)}
    omitted: list[str] = []
    selected: list[tuple[ConceptMeta, str]] = []
    for concept_id in ids:
        concept = known.get(concept_id)
        if concept is None:
            omitted.append(concept_id)
            continue
        path = bundle_path / concept.path
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            omitted.append(concept_id)
            continue
        body = _FRONTMATTER_RE.sub("", text, count=1).strip()
        selected.append((concept, body))

    blocks: list[str] = []
    used = 0
    for concept, body in selected[:max_concepts]:
        block = (
            f'<okf_concept id="{concept.concept}" type="{concept.type}" '
            f'status="{concept.status}" trust="{concept.trust}" '
            f'stale="{str(concept.stale).lower()}">\n'
            f"{body}\n"
            "</okf_concept>"
        )
        if len(block.encode("utf-8")) + used > max_bytes:
            omitted.append(concept.concept)
            continue
        blocks.append(block)
        used += len(block.encode("utf-8"))
    omitted.extend(c.concept for c, _b in selected[max_concepts:])
    return "\n\n".join(blocks), omitted


def _cell(value: object) -> str:
    text = str(value).replace("|", "\\|").replace("\n", " ")
    if len(text) > MAX_DESCRIPTION_CHARS:
        text = text[:MAX_DESCRIPTION_CHARS] + "..."
    return text


def render_concept_map(concepts: list[ConceptMeta]) -> str:
    """Render a model-facing routing table of every concept in a bundle."""
    if not concepts:
        return '<concept_map count="0">No concepts with OKF frontmatter.</concept_map>'
    shown = concepts[:MAX_SUMMARY_CONCEPTS]
    lines = [f'<concept_map count="{len(concepts)}">']
    lines.append("path | id | title | description | type | status | trust | stale")
    lines.append("--- | --- | --- | --- | --- | --- | --- | ---")
    lines.extend(
        " | ".join(
            (
                _cell(concept.path),
                _cell(concept.concept),
                _cell(concept.title),
                _cell(concept.description),
                _cell(concept.type),
                _cell(concept.status),
                _cell(concept.trust),
                "yes" if concept.stale else "no",
            )
        )
        for concept in shown
    )
    if len(concepts) > len(shown):
        lines.append(f"... and {len(concepts) - len(shown)} more concepts")
    lines.append("</concept_map>")
    return "\n".join(lines)


def summarize_bundle(bundle_path: Path) -> str:
    """Return a compact trust/status block for every concept in a bundle."""
    rows = [
        (
            concept.concept,
            concept.type,
            concept.status,
            concept.trust,
            "yes" if concept.stale else "no",
        )
        for concept in collect_concepts(bundle_path)
    ]

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
