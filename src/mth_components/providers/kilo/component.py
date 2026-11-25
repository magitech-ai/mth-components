"""Animas component entry point for the Kilo Code CLI model provider."""

from __future__ import annotations

from typing import Any

from animas import Context

from mth_components.providers.kilo.provider import (
    DEFAULT_AGENT,
    DEFAULT_MODEL,
    KiloAdapter,
    KiloProvider,
)


def apply(
    ctx: Context,
    model: str = DEFAULT_MODEL,
    *,
    binary: str = "kilo",
    timeout_seconds: float = 120.0,
    agent: str = DEFAULT_AGENT,
    **kwargs: Any,
) -> None:
    """Mount the keyless Kilo provider onto ``ctx.llm``.

    ``kilo-auto/free`` routes to a free model with no API key. A deployment may
    pin any of Kilo's 404 published model ids instead.
    """
    del kwargs
    ctx.llm.register_provider(
        "kilo",
        KiloProvider(
            model=model,
            binary=binary,
            timeout_seconds=timeout_seconds,
            agent=agent,
        ),
    )
    # The unified turn engine dispatches through an adapter, not the legacy
    # provider object; both are registered under the same provider id.
    ctx.llm.register_adapter(
        ["kilo"],
        KiloAdapter(binary=binary, timeout_seconds=timeout_seconds, agent=agent),
    )


apply.inject = ["llm"]  # type: ignore[attr-defined]
