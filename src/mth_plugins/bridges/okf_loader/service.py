"""OKF Session Catalog + Loader Tool service.

Injects catalog as <system-reminder> message and provides okf_bundle({name})
tool for model-driven bundle loading.
"""

from __future__ import annotations

from typing import Any

from anima import Context, Disposer, Service
from magitech_harness.tools.definition import define_tool


def _escape_xml(text: str) -> str:
    """XML-escape text for safe inclusion in catalog."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


class OKFSessionCatalogService(Service):
    """Session catalog and loader tool for OKF knowledge bundles.

    Injects catalog as <system-reminder> message at session start.
    Provides okf_bundle({name}) tool for model-driven bundle loading.
    """

    def __init__(self, ctx: Context):
        super().__init__(ctx, "okf_loader")
        self._prompt_disposer: Disposer | None = None
        self._tool_disposers: list[Disposer] = []
        self.ctx.effect(self._cleanup)

    def _build_catalog_xml(self) -> str:
        """Build Tier 1 catalog XML from registered bundles.

        Contains only names + descriptions (no bodies, paths, sources,
        providers, or routing hints). Sorted by name.
        """
        registry = self.ctx.okf_registry
        bundles = registry.list_bundles()
        if not bundles:
            return ""

        lines: list[str] = ["<okf_bundles>"]
        for bundle in bundles:
            lines.append("  <bundle>")
            lines.append(f"    <name>{_escape_xml(bundle.name)}</name>")
            lines.append(
                f"    <description>{_escape_xml(bundle.description)}</description>"
            )
            lines.append("  </bundle>")
        lines.append("</okf_bundles>")
        return "\n".join(lines)

    def _inject_catalog(self) -> None:
        """Inject catalog as <system-reminder> message via system prompt section."""
        catalog_xml = self._build_catalog_xml()
        if not catalog_xml:
            return

        behavioral_instructions = (
            "The following OKF knowledge bundles contain structured domain "
            "knowledge. When a task matches a bundle's description, use the "
            "okf_bundle({name}) tool to load the bundle's index.md before "
            "proceeding. When a concept references relative paths, resolve them "
            "against the bundle root (the directory containing index.md)."
        )

        catalog_block = (
            f"<system-reminder>\n{behavioral_instructions}\n\n"
            f"{catalog_xml}\n</system-reminder>"
        )

        system_prompt = self.ctx.get("systemPrompt", None, strict=False)
        if system_prompt is not None:
            self._prompt_disposer = system_prompt.register_section(
                "okf_catalog", catalog_block
            )

    def mount_tools(self) -> None:
        """Register okf_bundle({name}) tool onto ctx.tools."""
        if not self.ctx.has("tools"):
            return

        # Clear existing tool disposers before re-registering
        for d in self._tool_disposers:
            d()
        self._tool_disposers.clear()

        async def _handle_okf_bundle(args: dict[str, Any]) -> str:
            name = str(args.get("name", "")).strip()
            if not name:
                return "Error: okf_bundle requires a non-empty 'name' parameter."

            registry = self.ctx.okf_registry
            bundle = registry.get_bundle(name)
            if bundle is None:
                available = [b.name for b in registry.list_bundles()]
                return (
                    f"Error: OKF bundle '{name}' not found. "
                    f"Available bundles: {available}"
                )

            index_md = bundle.path / "index.md"
            if not index_md.is_file():
                return f"Error: OKF bundle '{name}' index.md not found at {bundle.path}"

            try:
                content = index_md.read_text(encoding="utf-8")
            except OSError as exc:
                return f"Error: Failed to read OKF bundle '{name}': {exc}"

            resource_base = str(bundle.path.resolve())

            return (
                f'<okf_bundle_content name="{_escape_xml(name)}">\n'
                f"{content}\n"
                f"</okf_bundle_content>\n"
                f"<resource_base>{_escape_xml(resource_base)}</resource_base>"
            )

        okf_bundle_tool = define_tool(
            name="okf_bundle",
            description=(
                "Load the full content of an OKF knowledge bundle by name. "
                "Use when a task matches a bundle's description."
            ),
            parameters={
                "name": {
                    "type": "string",
                    "description": "The unique name of the OKF bundle to load.",
                    "required": True,
                }
            },
            execute=_handle_okf_bundle,
        )
        self._tool_disposers.append(self.ctx.tools.register(okf_bundle_tool))

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
