"""Animas component entry point for repo verification."""

from __future__ import annotations

from typing import Any

from animas import Context
from magitech_harness.tools.schema import define_tool

from mth_components.tools.verify_repo.verify import handle_verify_repo


def apply(ctx: Context, config: dict | None = None, **kwargs: Any) -> None:
    ctx.tools.register(
        define_tool(
            name="verify_repo",
            description=(
                "Run the repository's declared verification checks "
                "(docs/agents/checks.md, or a Python default)."
            ),
            parameters={"path": {"type": "string"}},
            execute=handle_verify_repo,
        )
    )


apply.inject = ["tools"]  # type: ignore[attr-defined]
