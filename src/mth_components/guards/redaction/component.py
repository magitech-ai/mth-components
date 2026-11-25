"""Animas component entry point for the tool-output redaction guard."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from animas import Context

from mth_components.guards.redaction.redaction import (
    SECRET_ENV_NAMES,
    redact,
)


def apply(ctx: Context, config: dict | None = None, **kwargs: Any) -> None:
    """Mount a ``tools/post-execute`` guard that redacts secret values.

    Config:
        env_names: override the default environment-variable names.
        extra_env_names: additional names appended to the defaults.
    """
    cfg = {**(config or {}), **kwargs}
    names: tuple[str, ...] = tuple(cfg.get("env_names", SECRET_ENV_NAMES))
    extra = cfg.get("extra_env_names", ())
    if extra:
        names = names + tuple(extra)

    async def _post_execute(
        output: Any, call_data: Any, next_fn: Callable[..., Any]
    ) -> Any:
        res = await next_fn(output, call_data)

        # Legacy path: (raw_string_output, call_data) -> string.
        if isinstance(output, str):
            if not isinstance(res, str):
                return res
            return redact(res, names)

        # Parity pipeline: (exec_view, result) -> {"kind": "accept", ...}.
        result = call_data if isinstance(call_data, Mapping) else {}
        content = result.get("content")
        if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
            return res
        blocks: list[Any] = []
        for block in content:
            if (
                isinstance(block, Mapping)
                and block.get("type") in ("text", "reasoning")
                and isinstance(block.get("text"), str)
            ):
                blocks.append({**block, "text": redact(block["text"], names)})
            else:
                blocks.append(block)
        return {"kind": "accept", "content": blocks}

    ctx.on("tools/post-execute", _post_execute)


apply.inject = ["tools"]  # type: ignore[attr-defined]
