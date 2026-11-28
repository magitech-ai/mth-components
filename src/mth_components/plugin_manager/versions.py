"""Registry version projections.

Ports ``packages/boot/plugin-manager/src/versions.ts``: published newer
versions from ``pnpm view`` answers and per-version compatibility facts.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import cmp_to_key
from typing import Any

from mth_components.plugin_manager.semver import gt, is_valid, prerelease, rcompare

VERSION_LIST_FIELDS = ("versions", "time", "dist-tags")
VERSION_FIELDS = ("name", "version", "engines", "peerDependencies", "deprecated", "dsh")


def newer_versions(answer: Mapping[str, Any], installed: str) -> list[dict[str, Any]]:
    """Valid versions strictly newer than ``installed``, newest first."""
    raw = answer.get("versions")
    versions = [raw] if isinstance(raw, str) else raw
    if not isinstance(versions, list):
        raise ValueError("pnpm view answered no versions")
    raw_times = answer.get("time")
    raw_tags = answer.get("dist-tags")
    times: dict[str, Any] = raw_times if isinstance(raw_times, dict) else {}
    tags: dict[str, Any] = raw_tags if isinstance(raw_tags, dict) else {}
    selected = [
        version
        for version in versions
        if isinstance(version, str) and is_valid(version) and gt(version, installed)
    ]
    selected.sort(key=_RCOMPARE_KEY)
    result: list[dict[str, Any]] = []
    for version in selected:
        item: dict[str, Any] = {
            "version": version,
            "tags": [tag for tag, value in tags.items() if value == version],
            "prerelease": prerelease(version) is not None,
            "bundle": False,
        }
        published = times.get(version)
        if isinstance(published, str):
            item["publishedAt"] = published
        result.append(item)
    return result


_RCOMPARE_KEY = cmp_to_key(rcompare)


def version_facts(answer: Mapping[str, Any], name: str, version: str) -> dict[str, Any]:
    """Compatibility-relevant facts for one published version."""
    facts: dict[str, Any] = {
        "name": answer.get("name") if isinstance(answer.get("name"), str) else name,
        "version": (
            answer.get("version") if isinstance(answer.get("version"), str) else version
        ),
        "bundle": isinstance(answer.get("dsh"), dict)
        and isinstance(answer["dsh"].get("bundle"), dict),
    }
    for key in ("engines", "peerDependencies"):
        if key in answer:
            facts[key] = answer[key]
    deprecated = answer.get("deprecated")
    if isinstance(deprecated, str) and deprecated:
        facts["deprecated"] = deprecated
    return facts


__all__ = ["VERSION_FIELDS", "VERSION_LIST_FIELDS", "newer_versions", "version_facts"]
