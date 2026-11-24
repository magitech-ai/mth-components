"""Animas component entry point for Python source analysis tools."""

from __future__ import annotations

from typing import Any

from animas import Context
from magitech_harness.tools.schema import define_tool

from mth_plugins.tools.code_analysis.handlers import (
    handle_code_metrics,
    handle_find_duplicates,
    handle_generate_docs,
)


def apply(ctx: Context, config: dict | None = None, **kwargs: Any) -> None:
    ctx.tools.register(
        define_tool(
            name="code_metrics",
            description=(
                "Calculate code quality metrics for Python files in a directory."
            ),
            parameters={"path": {"type": "string"}},
            execute=handle_code_metrics,
        )
    )
    ctx.tools.register(
        define_tool(
            name="find_duplicates",
            description="Find duplicate code blocks across Python files.",
            parameters={
                "path": {"type": "string"},
                "min_lines": {"type": "integer"},
            },
            execute=handle_find_duplicates,
        )
    )
    ctx.tools.register(
        define_tool(
            name="generate_docs",
            description="Generate markdown documentation from Python docstrings.",
            parameters={"path": {"type": "string"}},
            execute=handle_generate_docs,
        )
    )


apply.inject = ["tools"]  # type: ignore[attr-defined]
