"""Skills Session Catalog + Loader Tool service.

Injects catalog as <system-reminder> message and provides skill({name})
tool for model-driven skill loading.
"""

from __future__ import annotations

from typing import Any

from animas import Context, Disposer, Service
from magitech_harness.tools.schema import define_tool

from mth_components.bridges.skills_registry.service import SkillsProviderRegistryService


def _escape_xml(text: str) -> str:
    """XML-escape text for safe inclusion in catalog."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


class SkillsSessionCatalogService(Service):
    """Session catalog and loader tool for Agent Skills.

    Injects catalog as <system-reminder> message at session start.
    Provides skill({name}) tool for model-driven skill loading.
    """

    def __init__(self, ctx: Context):
        super().__init__(ctx, "skills_loader")
        self._prompt_disposer: Disposer | None = None
        self._tool_disposers: list[Disposer] = []
        self.ctx.effect(self._cleanup)

    def _build_catalog_xml(self) -> str:
        """Build Tier 1 catalog XML from registered skills.

        Contains only names + descriptions (no bodies, paths, sources,
        providers, or routing hints). Sorted by name.
        """
        registry = self.ctx.skills_registry
        # Per the Agent Skills spec, user-invoked skills (disable-model-invocation)
        # are hidden entirely from the model-facing catalog.
        skills = registry.list_model_invocable()
        if not skills:
            return ""

        lines: list[str] = ["<skills>"]
        for skill in skills:
            lines.append("  <skill>")
            lines.append(f"    <name>{_escape_xml(skill.name)}</name>")
            lines.append(
                f"    <description>{_escape_xml(skill.description)}</description>"
            )
            lines.append("  </skill>")
        lines.append("</skills>")
        return "\n".join(lines)

    def _inject_catalog(self) -> None:
        """Inject catalog as <system-reminder> message via system prompt section."""
        catalog_xml = self._build_catalog_xml()
        if not catalog_xml:
            return

        behavioral_instructions = (
            "The following Agent Skills provide specialized capabilities for "
            "specific tasks. When a task matches a skill's description, use the "
            "skill({name}) tool to load the SKILL.md instructions before proceeding."
        )

        catalog_block = (
            f"<system-reminder>\n{behavioral_instructions}\n\n"
            f"{catalog_xml}\n</system-reminder>"
        )

        system_prompt = self.ctx.get("systemPrompt", None, strict=False)
        if system_prompt is not None:
            self._prompt_disposer = system_prompt.register_section(
                "skills_catalog", catalog_block
            )

    def mount_tools(self) -> None:
        """Register skill({name}) tool onto ctx.tools."""
        if not self.ctx.has("tools"):
            return

        for d in self._tool_disposers:
            d()
        self._tool_disposers.clear()

        async def _handle_skill(args: dict[str, Any]) -> str:
            name = str(args.get("name", "")).strip()
            if not name:
                return "Error: skill requires a non-empty 'name' parameter."

            registry: SkillsProviderRegistryService = self.ctx.skills_registry
            skill = registry.get_skill(name)
            if skill is None:
                available = [s.name for s in registry.list_model_invocable()]
                return f"Error: Skill '{name}' not found. Available skills: {available}"
            if not skill.invocation.model_invocable:
                return (
                    f"Error: Skill '{name}' is not model-invocable "
                    "(disable-model-invocation)."
                )

            return skill.body

        skill_tool = define_tool(
            name="skill",
            description=(
                "Load the full instructions of an Agent Skill by name. "
                "Use when a task matches a skill's description."
            ),
            parameters={
                "name": {
                    "type": "string",
                    "description": "The unique name of the skill to load.",
                    "required": True,
                }
            },
            execute=_handle_skill,
        )
        self._tool_disposers.append(self.ctx.tools.register(skill_tool))

    def _cleanup(self) -> None:
        if self._prompt_disposer is not None:
            self._prompt_disposer()
            self._prompt_disposer = None
        for d in self._tool_disposers:
            d()
        self._tool_disposers.clear()

    def activate(self) -> None:
        """Activate the service: inject catalog and mount tools."""
        self._inject_catalog()
        self.mount_tools()
