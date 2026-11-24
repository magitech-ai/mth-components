"""Animas component entry point for the keyless web search tool."""

from __future__ import annotations

from typing import Any

from animas import Context
from magitech_harness.tools.schema import define_tool

from mth_plugins.tools.web_search.handlers import handle_web_search


def apply(ctx: Context, config: dict | None = None, **kwargs: Any) -> None:
    ctx.tools.register(
        define_tool(
            name="web_search",
            description=(
                "Search the web via SearXNG (if configured) or DuckDuckGo. "
                "Uses SEARXNG_URL first when set; no API key required."
            ),
            parameters={"query": {"type": "string", "required": True}},
            execute=handle_web_search,
        )
    )


apply.inject = ["tools"]  # type: ignore[attr-defined]
