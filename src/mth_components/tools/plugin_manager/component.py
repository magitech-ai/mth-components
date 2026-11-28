"""Agent-facing profile plugin management tool (DSH row 1).

Ports ``packages/boot/plugin-manager/src/tools.ts``: one ``plugin_manager``
tool whose ``application`` field is authoritative and whose ``nextStep`` tells
the model what to do for every non-applied outcome. Every action escalates
to ``danger-full-access`` through the harness approval service.
"""

from __future__ import annotations

import json
from typing import Any

from animas import Context
from magitech_harness import __version__
from magitech_harness.sandbox.escalation import approve_escalation
from magitech_harness.tools.schema import ToolArgsError, define_tool

ERROR_STEPS = {
    "management-required": (
        "This entry belongs to plugin management itself and cannot be changed."
    ),
    "unaddressable": (
        "This entry has no unique profile patch target; tell the user to edit "
        "the profile patch file directly."
    ),
    "unknown-plugin": "Call list_plugins and retry with an exact entryId.",
    "invalid-spec": (
        "Retry with an npm package name, a git URL, a tarball URL, or an "
        "absolute local directory."
    ),
    "already-installed": (
        "The package is already installed. To reload its files after an in-place "
        "change, call set_plugin with enabled true on its entry from list_plugins; "
        "to install another version, call remove_bundle first, then install_bundle "
        "again."
    ),
    "ambiguous-install": (
        "The dependency change named no single package; call list_bundles to see "
        "what was installed."
    ),
    "not-bundle": (
        "The package declares no dsh.bundle and cannot be managed as a plugin; "
        "install a DSH plugin bundle instead."
    ),
    "not-removable": "Call list_bundles; only bundles marked removable can be removed.",
    "stop-profile": (
        "This profile runs without HMR; tell the user to stop DSH and run "
        "`dsh plugin --profile <profile> remove <bundle>`."
    ),
    "bundle-in-use": (
        "Call list_plugins, disable the entries from other layers that still "
        "use this bundle, then retry."
    ),
    "stale-approval": (
        "Call install_bundle again without approvedBuilds to read the current "
        "pendingBuilds."
    ),
    "incompatible-version": (
        "Install a version compatible with the running DSH. A version exemption "
        "needs the user's explicit acceptance of crash and data-loss risk."
    ),
    "unsupported-platform": (
        "This bundle cannot run on the current Host platform; ask the user to "
        "use a supported Host."
    ),
    "unexpected-package": (
        "The source installed another package or version than requested and was "
        "rolled back; report this to the user."
    ),
    "operation-error": (
        "Report the diagnostic to the user; retry only after its cause is addressed."
    ),
}

FAILURE_STEPS = {
    "pnpm-missing": "pnpm is not available to DSH; tell the user to install pnpm.",
    "timeout": "Retry, or retry with another registry.",
    "not-found": "Check the package name and version with the user, then retry.",
    "no-matching-version": "Retry with a published version.",
    "network": "Retry later or pass another registry.",
    "disk-full": "Tell the user to free disk space, then retry.",
    "permission": (
        "Tell the user DSH cannot write the profile directory; retry after they "
        "fix its permissions."
    ),
    "build-blocked": (
        "Ask the user whether to allow the install scripts of the packages in "
        "pendingBuilds; if they approve, retry with approvedBuilds."
    ),
}

RESTART_STEPS = {
    "no-hmr": "this profile applies changes only at startup",
    "package-moved": (
        "an installed package now resolves to another directory, or the change "
        "replaces a package the DSH installation provides; remove a package "
        "before installing it from another location"
    ),
    "host-module": "the change reaches modules DSH itself loaded",
    "unmanaged-plugin": ("the change reaches a plugin running outside the plugin list"),
    "module-comparison": ("DSH could not compare loaded plugins with their files"),
}

ACTIONS = (
    "list_plugins",
    "list_bundles",
    "set_plugin",
    "set_bundle",
    "install_bundle",
    "remove_bundle",
    "list_version_exemptions",
    "set_version_exemption",
)


def with_next_step(result: dict[str, Any]) -> dict[str, Any]:
    """Add the model-facing next step for a non-applied change."""
    application = result.get("application")
    if application == "applied":
        return result
    if application == "cancelled":
        next_step = (
            "The installation was cancelled and its files restored; nothing changed."
        )
    elif application == "overridden":
        next_step = (
            "Saved, but a higher-priority layer such as the agent preset overrides "
            "it; tell the user it is not in effect."
        )
    elif application == "restart-required":
        reason = result.get("restartReason")
        suffix = "" if reason is None else f": {RESTART_STEPS.get(reason, reason)}"
        next_step = f"Tell the user to restart DSH to apply this change{suffix}."
    else:
        error = result.get("error") or {}
        code = error.get("code", "operation-error")
        package_result = result.get("packageResult") or {}
        kind = package_result.get("kind") if code == "operation-error" else None
        next_step = (
            FAILURE_STEPS.get(kind) if kind is not None else None
        ) or ERROR_STEPS.get(code, ERROR_STEPS["operation-error"])
    return {**result, "nextStep": next_step}


def _strip_meta(rows: list[dict[str, Any]], bundles: bool) -> list[dict[str, Any]]:
    """Remove GUI-only fields from a listed row."""
    result: list[dict[str, Any]] = []
    for row in rows:
        clean = {key: value for key, value in row.items() if key != "meta"}
        if bundles:
            clean.pop("update", None)
            clean.pop("listedByDefault", None)
            clean["rows"] = [
                {key: value for key, value in item.items() if key != "meta"}
                for item in row.get("rows", [])
                if isinstance(item, dict)
            ]
        result.append(clean)
    return result


async def _execute(  # noqa: PLR0912
    ctx: Context, args: dict[str, Any], exec_ctx: Any
) -> str:
    manager = ctx.get("pluginManager", None, strict=False)
    approver = ctx.get("approval", None, strict=False)
    agent = getattr(exec_ctx, "agent", None)
    if approver is None or agent is None:
        raise ToolArgsError(
            ["plugin management requires an approval service and an agent"]
        )
    policy_service = ctx.get("sandboxPolicy", None, strict=False)
    mode = (
        str(policy_service.resolve(agent.session).get("mode", "workspace-write"))
        if policy_service is not None
        else "workspace-write"
    )
    await approve_escalation(
        {
            "requestedMode": "danger-full-access",
            "effectiveMode": mode,
            "subject": "plugin management operation",
            "justification": (
                f"plugin_manager {json.dumps(args)}. "
                "Profile changes persist across sessions."
            ),
        },
        {
            "approver": approver,
            "agent": agent,
            "callId": getattr(exec_ctx, "call_id", None)
            or getattr(exec_ctx, "callId", None),
            "toolName": "plugin_manager",
            "signal": getattr(exec_ctx, "signal", None),
        },
    )
    if manager is None:
        raise ToolArgsError(["pluginManager is not mounted on this context"])
    signal = getattr(exec_ctx, "signal", None)
    if signal is not None and getattr(signal, "aborted", False):
        raise ToolArgsError(["operation cancelled"])
    action = args.get("action")
    if action not in ACTIONS:
        raise ToolArgsError(
            ["action must be one of the declared management operations"]
        )

    if action == "list_version_exemptions":
        return json.dumps(
            {"runtimeVersion": __version__, **manager.list_version_exemptions()}
        )
    if action == "set_version_exemption":
        missing = [
            name
            for name in ("target", "runtimeVersion", "enabled")
            if args.get(name) is None
        ]
        if missing:
            raise ToolArgsError([f"{name} is required" for name in missing])
        return json.dumps(
            with_next_step(
                await manager.set_version_exemption(
                    args["target"],
                    args["runtimeVersion"],
                    args["enabled"],
                    args.get("acceptRisk", False),
                )
            )
        )
    if action in ("list_plugins", "list_bundles"):
        offset = args.get("offset", 0)
        limit = args.get("limit", 25)
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ToolArgsError(
                [
                    "offset must be a non-negative integer and limit must be "
                    "an integer from 1 to 100"
                ]
            )
        rows = (
            await manager.list_plugins()
            if action == "list_plugins"
            else await manager.list_bundles()
        )
        entries = _strip_meta(rows[offset : offset + limit], action == "list_bundles")
        next_offset = (
            offset + len(entries) if offset + len(entries) < len(rows) else None
        )
        return json.dumps(
            {"entries": entries, "total": len(rows), "nextOffset": next_offset}
        )
    if action in ("set_plugin", "set_bundle"):
        if args.get("target") is None or args.get("enabled") is None:
            raise ToolArgsError(["target and enabled are required"])
        result = (
            await manager.set_plugin_enabled(args["target"], args["enabled"])
            if action == "set_plugin"
            else await manager.set_bundle_enabled(args["target"], args["enabled"])
        )
        return json.dumps(with_next_step(result))
    if action == "install_bundle":
        if args.get("target") is None:
            raise ToolArgsError(["target package spec is required"])
        options = {
            key: args[key]
            for key in ("enabled", "approvedBuilds", "registry")
            if key in args
        }
        return json.dumps(
            with_next_step(await manager.install_bundle(args["target"], options))
        )
    if args.get("target") is None:
        raise ToolArgsError(["target bundle name is required"])
    return json.dumps(with_next_step(await manager.remove_bundle(args["target"])))


def apply(ctx: Context, **kwargs: Any) -> None:
    """Register the single ``plugin_manager`` tool on the context."""
    del kwargs

    ctx.tools.register(
        define_tool(
            name="plugin_manager",
            description=(
                "List plugins or bundles in the current profile, enable or disable "
                "them, "
                "install a bundle, or remove an installed bundle. Changes affect every "
                "session in this profile. The application field of a change result is "
                "authoritative; any other value carries nextStep."
            ),
            parameters={
                "action": {
                    "type": "string",
                    "required": True,
                    "enum": list(ACTIONS),
                },
                "target": {"type": "string"},
                "enabled": {"type": "boolean"},
                "runtimeVersion": {"type": "string"},
                "acceptRisk": {"type": "boolean"},
                "approvedBuilds": {"type": "array", "items": {"type": "string"}},
                "registry": {"type": "string"},
                "offset": {"type": "number"},
                "limit": {"type": "number"},
            },
            output={
                "schema": {"type": "string"},
                "render": lambda _args, value: [{"type": "text", "text": str(value)}],
            },
            execute=lambda args, run_ctx: _execute(ctx, args, run_ctx),
            presentCall=lambda args: {
                "card": "generic",
                "title": "Manage profile plugins",
                "kind": "read"
                if str(args.get("action", "")).startswith("list_")
                else "other",
                "rawInput": args,
            },
        )
    )


apply.inject = ["tools", "pluginManager", "sandboxPolicy"]  # type: ignore[attr-defined]

__all__ = [
    "ACTIONS",
    "ERROR_STEPS",
    "FAILURE_STEPS",
    "RESTART_STEPS",
    "_execute",
    "apply",
    "with_next_step",
]
