"""Skills Provider Registry service.

Pure provider registry for Agent Skills. Owns conflict resolution
(first-wins by rank) and consumer access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from animas import Context, Service


@dataclass(frozen=True)
class SkillInvocation:
    """Independent model-side and user-side invocation policy per agentskills.io."""

    model_invocable: bool = True
    user_invocable: bool = True


@dataclass(frozen=True)
class SkillBundleInfo:
    """Represents a discovered Agent Skill package."""

    name: str
    description: str
    path: Any  # skill directory
    skill_file: Any  # SKILL.md file path
    body: str
    invocation: SkillInvocation = field(default_factory=SkillInvocation)
    rank: int = 0


class SkillsProviderRegistryService(Service):
    """Pure provider registry for Agent Skills.

    Owns conflict resolution (first-wins by rank) and consumer access.
    """

    def __init__(self, ctx: Context):
        super().__init__(ctx, "skills_registry")
        self.skills: dict[str, SkillBundleInfo] = {}
        self.ctx.effect(self._cleanup)

    def register_skill(self, skill: SkillBundleInfo) -> bool:
        """Register a skill. First-wins by rank for same-name skills.

        Lower rank number = higher priority. Existing skill wins ties.
        """
        existing = self.skills.get(skill.name)
        if existing is not None:
            if skill.rank < existing.rank:
                self.skills[skill.name] = skill
                return True
            return False
        self.skills[skill.name] = skill
        return True

    def get_skill(self, name: str) -> SkillBundleInfo | None:
        return self.skills.get(name)

    def list_skills(self) -> list[SkillBundleInfo]:
        return sorted(self.skills.values(), key=lambda s: s.name)

    def list_model_invocable(self) -> list[SkillBundleInfo]:
        """Skills the model may discover and load (catalog + skill tool)."""
        return [s for s in self.list_skills() if s.invocation.model_invocable]

    def list_user_invocable(self) -> list[SkillBundleInfo]:
        """Skills a human may load explicitly (/name)."""
        return [s for s in self.list_skills() if s.invocation.user_invocable]

    def load_for_user(self, name: str) -> str:
        """Return a user-invoked skill's body for explicit (/name) activation."""
        skill = self.skills.get(name)
        if skill is None:
            available = [s.name for s in self.list_user_invocable()]
            return f"Error: skill '{name}' not found. Available skills: {available}"
        if not skill.invocation.user_invocable:
            return f"Error: Skill '{name}' is not user-invocable."
        return skill.body

    def _cleanup(self) -> None:
        self.skills.clear()
