"""Tests for OKF bundle trust/status summaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from mth_plugins.bridges.okf_loader.trust import (
    MAX_SUMMARY_CONCEPTS,
    ConceptMeta,
    collect_concepts,
    is_stale,
    load_concept_bodies,
    parse_frontmatter,
    render_concept_map,
    summarize_bundle,
    trust_tier,
)


def test_parse_frontmatter() -> None:
    assert parse_frontmatter("no frontmatter") == {}
    assert parse_frontmatter("---\nnot: [valid\n---\n") == {}
    assert parse_frontmatter("---\n- a list\n---\n") == {}
    assert (
        parse_frontmatter("---\ntype: Policy\nstatus: stable\n---\nbody")["type"]
        == "Policy"
    )


def test_trust_tier() -> None:
    assert trust_tier({}) == "unverified"
    assert (
        trust_tier({"verified": [{"by": "team:x", "at": "2026-01-02"}]})
        == "machine-confirmed"
    )
    assert (
        trust_tier({"verified": [{"by": "human:alice", "at": "2026-01-02"}]})
        == "human-reviewed"
    )
    assert (
        trust_tier({"verified": {"by": "human:alice", "at": "2026-01-02"}})
        == "human-reviewed"
    )
    generated = {"by": "process:x", "at": "2026-01-03"}
    assert (
        trust_tier(
            {
                "generated": generated,
                "verified": [{"by": "human:a", "at": "2026-01-02"}],
            }
        )
        == "unverified"
    )
    assert trust_tier({"verified": [{"by": "human:a"}]}) == "unverified"
    assert trust_tier({"verified": ["not a mapping"]}) == "unverified"


def test_is_stale() -> None:
    assert is_stale({}) is False
    assert is_stale({"stale_after": "not a date"}) is False
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert is_stale({"stale_after": past}) is True
    assert is_stale({"stale_after": future}) is False
    assert is_stale({"stale_after": "2026-01-01T00:00:00Z"}) is True


def test_summarize_bundle(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text("---\nokf_version: '0.3'\n---\n")
    (tmp_path / "log.md").write_text("log")
    (tmp_path / "plain.md").write_text("no frontmatter")
    (tmp_path / "a.md").write_text("---\ntype: Policy\ntitle: A\n---\nbody")
    (tmp_path / "b.md").write_text(
        "---\ntype: Procedure\nstatus: draft\n"
        "generated: {by: 'process:x', at: '2026-01-01'}\n"
        "verified: [{by: 'human:a', at: '2026-01-02'}]\n"
        'stale_after: "2000-01-01T00:00:00Z"\n---\nbody'
    )
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "c.md").write_text("---\ntype: Reference\n---\n")
    summary = summarize_bundle(tmp_path)
    assert 'count="2"' in summary
    assert "a | Policy | stable | unverified | no" in summary
    assert "b | Procedure | draft | human-reviewed | yes" in summary
    assert ".hidden" not in summary
    assert "index" not in summary and "log" not in summary


def test_summarize_bundle_empty_and_cap(tmp_path: Path) -> None:
    assert "No concepts" in summarize_bundle(tmp_path)
    for i in range(45):
        (tmp_path / f"c{i:02}.md").write_text("---\ntype: Reference\n---\n")
    summary = summarize_bundle(tmp_path)
    assert 'count="45"' in summary
    assert "and 5 more concepts" in summary


def test_summarize_bundle_unreadable_file(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "bad.md").write_text("---\ntype: Policy\n---\n")
    (tmp_path / "good.md").write_text("---\ntype: Policy\n---\n")
    original = Path.read_text

    def flaky_read(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "bad.md":
            raise OSError("unreadable")
        return original(self, *args, **kwargs)  # type: ignore[return-value]

    monkeypatch.setattr(Path, "read_text", flaky_read)
    summary = summarize_bundle(tmp_path)
    assert 'count="1"' in summary
    assert "good | Policy | stable | unverified | no" in summary


def test_render_concept_map_rows_and_cap() -> None:
    concepts = [
        ConceptMeta(
            path="policies/rule.md",
            concept="rule",
            title="Rule | one",
            description="A short description",
            type="Policy",
            status="stable",
            trust="human-reviewed",
            stale=False,
        ),
        ConceptMeta(
            path="decisions/d-001.md",
            concept="d-001",
            title="First decision",
            description="Another description",
            type="Decision",
            status="stable",
            trust="unverified",
            stale=True,
        ),
    ]
    rendered = render_concept_map(concepts)
    assert '<concept_map count="2">' in rendered
    assert "path | id | title | description | type | status | trust | stale" in rendered
    assert (
        "policies/rule.md | rule | Rule \\| one | A short description "
        "| Policy | stable | human-reviewed | no" in rendered
    )
    assert (
        "decisions/d-001.md | d-001 | First decision | Another description "
        "| Decision | stable | unverified | yes" in rendered
    )
    assert "</concept_map>" in rendered


def test_render_concept_map_empty_and_caps() -> None:
    assert 'count="0"' in render_concept_map([])
    long = ConceptMeta(
        path="p.md",
        concept="p",
        title="T",
        description="x" * 300,
        type="Policy",
        status="stable",
        trust="unverified",
        stale=False,
    )
    assert "..." in render_concept_map([long])
    many = [
        ConceptMeta(
            path=f"c{i}.md",
            concept=f"c{i}",
            title="T",
            description="d",
            type="Policy",
            status="stable",
            trust="unverified",
            stale=False,
        )
        for i in range(MAX_SUMMARY_CONCEPTS + 2)
    ]
    rendered = render_concept_map(many)
    assert f'count="{MAX_SUMMARY_CONCEPTS + 2}"' in rendered
    assert "... and 2 more concepts" in rendered


def test_load_concept_bodies_selection_caps_and_notices(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    (root / "policies").mkdir(parents=True)
    (root / "policies" / "a.md").write_text(
        "---\ntype: Policy\ntitle: A\n---\nBody A", encoding="utf-8"
    )
    (root / "policies" / "b.md").write_text(
        "---\ntype: Policy\ntitle: B\n---\nBody B", encoding="utf-8"
    )
    rendered, omitted = load_concept_bodies(root, ["a", "missing"])
    assert '<okf_concept id="a"' in rendered
    assert "Body A" in rendered
    assert "---" not in rendered.split("<okf_concept")[1].split(">")[1][:40]
    assert omitted == ["missing"]
    assert '<okf_concept id="b"' not in rendered

    rendered, omitted = load_concept_bodies(root, ["a", "b"], max_concepts=1)
    assert "Body B" not in rendered
    assert omitted == ["b"]

    rendered, omitted = load_concept_bodies(root, ["a", "b"], max_bytes=10)
    assert "Body A" not in rendered
    assert omitted == ["a", "b"]


def test_load_concept_bodies_unreadable(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "bundle"
    (root / "policies").mkdir(parents=True)
    (root / "policies" / "a.md").write_text(
        "---\ntype: Policy\ntitle: A\n---\nBody", encoding="utf-8"
    )
    original = Path.read_text

    def flaky(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "a.md" and "Body" in original(self, *args, **kwargs):
            raise OSError("denied")
        return original(self, *args, **kwargs)  # type: ignore[return-value]

    monkeypatch.setattr(Path, "read_text", flaky)
    # collect_concepts skips the unreadable file entirely -> unknown ID.
    rendered, omitted = load_concept_bodies(root, ["a"])
    assert rendered == ""
    assert omitted == ["a"]


def test_collect_concepts_skips_path_outside_bundle(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("---\ntype: Policy\ntitle: Outside\n---\n", encoding="utf-8")
    (root / "escape.md").symlink_to(outside)
    assert collect_concepts(root) == []


def test_load_concept_bodies_read_failure_after_discovery(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "a.md").write_text(
        "---\ntype: Policy\ntitle: A\n---\nBody", encoding="utf-8"
    )
    original = Path.read_text
    calls = [0]

    def flaky(self: Path, *args: object, **kwargs: object) -> str:
        calls[0] += 1
        if calls[0] == 2:
            raise OSError("denied")
        return original(self, *args, **kwargs)  # type: ignore[return-value]

    monkeypatch.setattr(Path, "read_text", flaky)
    rendered, omitted = load_concept_bodies(root, ["a"])
    assert rendered == ""
    assert omitted == ["a"]
