"""Skills Filesystem Provider service.

Scans roots for directories containing SKILL.md with name and description
frontmatter. Registers discovered skills with the skills provider registry.


Per the Agent Skills spec: discovery is one directory level deep; name and
description are required; ``disable-model-invocation`` and ``user-invocable``
control the invocation policy; an invalid boolean spelling drops the skill
(fail-closed). Scope precedence is project > home > user, deduplicated by
resolved path so symlink aliases collapse.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from animas import Context, Service

from mth_components.bridges.skills_registry.service import (
    SkillBundleInfo,
    SkillInvocation,
)

# Lower rank number = higher priority (matches registry first-wins-by-rank).
_SCOPE_RANK = {"project": 0, "home": 1, "user": 2, "custom": 3}


def _parse_bool(value: str | None, default: bool) -> bool | None:
    """Parse an Agent Skills boolean flag; None means invalid spelling."""
    if value is None:
        return default
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return None


def _parse_skill_frontmatter(text: str) -> dict[str, Any] | None:
    """Parse SKILL.md frontmatter. Returns None when unusable (fail-closed)."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    raw = text[4:end]
    fields: dict[str, str] = {}
    for raw_line in raw.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, val = stripped.split(":", 1)
        fields[key.strip()] = val.strip().strip('"').strip("'")

    name = fields.get("name", "").strip()
    description = fields.get("description", "").strip()
    if not name or not description:
        return None

    disable_model = _parse_bool(fields.get("disable-model-invocation"), False)
    user_invocable = _parse_bool(fields.get("user-invocable"), True)
    # Invalid boolean spelling drops the skill entirely (fail-closed).
    if disable_model is None or user_invocable is None:
        return None

    body = text[end + 5 :].strip()
    return {
        "name": name,
        "description": description,
        "invocation": SkillInvocation(
            model_invocable=not disable_model,
            user_invocable=user_invocable,
        ),
        "body": body,
    }


class SkillsFilesystemProviderService(Service):
    """Local filesystem provider for Agent Skills.

    Scans roots one level deep for <name>/SKILL.md. Registers discovered
    skills with the skills provider registry using scope-precedence
    deduplication by resolved path.
    """

    def __init__(self, ctx: Context):
        super().__init__(ctx, "skills_filesystem")
        self._discovered: list[str] = []
        self.ctx.effect(self._cleanup)

    def discover_skills(self, roots: list[tuple[Path, str]] | None = None) -> list[str]:  # noqa: PLR0912
        """Scan ``(root, scope)`` pairs for skills; return discovered names.

        Accepts the same ``(root, scope)`` shape as the native discovery so
        scope precedence and symlink dedup match the Agent Skills model.
        """
        if roots is None:
            roots = [(Path(".agents/skills"), "project")]
        if not roots:
            return []

        candidates: list[tuple[Path, SkillBundleInfo]] = []
        seen_paths: set[Path] = set()
        for root, scope in roots:
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                skill_file = child / "SKILL.md"
                if not skill_file.is_file():
                    continue
                try:
                    text = skill_file.read_text(encoding="utf-8")
                except OSError:
                    continue
                parsed = _parse_skill_frontmatter(text)
                if parsed is None:
                    continue
                resolved = skill_file.resolve()
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)
                # Fail-closed: unknown scopes get the lowest priority.
                rank = _SCOPE_RANK.get(scope, 3)
                # Lower rank number = higher priority.
                info = SkillBundleInfo(
                    name=parsed["name"],
                    description=parsed["description"],
                    path=child,
                    skill_file=skill_file,
                    body=parsed["body"],
                    invocation=parsed["invocation"],
                    rank=rank,
                )
                candidates.append((resolved, info))

        # First-wins by best scope rank per name (mirrors native precedence).
        best: dict[str, SkillBundleInfo] = {}
        for _resolved, info in candidates:
            existing = best.get(info.name)
            if existing is None or info.rank < existing.rank:
                best[info.name] = info

        registry = self.ctx.skills_registry
        discovered: list[str] = []
        for name in sorted(best):
            if registry.register_skill(best[name]):
                discovered.append(name)  # noqa: PERF401 - registration side effect
        self._discovered = discovered
        return discovered

    def _cleanup(self) -> None:
        self._discovered.clear()
