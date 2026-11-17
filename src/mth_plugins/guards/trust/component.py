"""Anima component entry point for workspace-trust-gate."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from anima import Context

from mth_plugins.guards.trust.gate import WorkspaceTrustGate

TOOLS_PRE_EXECUTE = "tools/pre-execute"


def _workspace_root(ctx: Context) -> Path:
    """Return the effective workspace root for trust decisions.

    Prefers the harness working-directory service (``ctx.workingDirectory``)
    when mounted; otherwise falls back to the process working directory.
    """
    wd = ctx.get("workingDirectory", None, strict=False)
    if wd is not None:
        directory = getattr(wd, "default_directory", None) or getattr(
            wd, "defaultDirectory", None
        )
        if isinstance(directory, str) and directory:
            return Path(directory)
    return Path.cwd()


def apply(
    ctx: Context,
    store_path: Path | str | None = None,
    **kwargs: Any,
) -> None:
    """Anima component mounting WorkspaceTrustGate onto the context.

    Mounts the trust gate as ``ctx.trust`` and registers it as a
    ``tools/pre-execute`` waterfall guard (ADR 0011: trust gate first,
    fail-closed). Every tool execution is checked against workspace
    trust before dispatch; an untrusted or unspecified workspace is
    denied fail-closed, and denials are audited via ``trust/denied``.
    """
    ctx.use(WorkspaceTrustGate, store_path=store_path, **kwargs)

    async def _on_tools_pre_execute(
        exec_view: dict[str, Any], next_fn: Callable[..., Any]
    ) -> Any:
        return await _pre_execute_handler(ctx, exec_view, next_fn)

    ctx.on(TOOLS_PRE_EXECUTE, _on_tools_pre_execute)


async def _pre_execute_handler(
    ctx: Context, exec_view: dict[str, Any], next_fn: Callable[..., Any]
) -> Any:
    """Evaluate workspace trust before a tool executes (pre-execute guard).

    Trust gate first, fail-closed: when the workspace is untrusted or
    unspecified and no interactive approval is available, the tool is
    denied and the denial is audited via ``trust/denied``.
    """
    tool_name = exec_view.get("name", "unknown")
    workspace = _workspace_root(ctx)
    gate = ctx.get("trust", None, strict=False)
    trusted = False
    if gate is not None:
        trusted = await gate.check_project_trust(workspace, prompt_user=False)
    if not trusted:
        ctx.emit(
            "trust/denied",
            {
                "tool": tool_name,
                "workspace": str(workspace),
                "reason": "workspace is not trusted",
            },
        )
        return {"kind": "deny", "reason": f"workspace '{workspace}' is not trusted"}
    return await next_fn(exec_view)


__all__ = ["TOOLS_PRE_EXECUTE", "_pre_execute_handler", "apply"]
