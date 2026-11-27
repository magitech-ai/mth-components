"""Plugin/runtime compatibility evaluation.

Ports ``packages/boot/app-boot/src/plugin-compatibility.ts``: an own
``engines.dsh`` range decides alone; otherwise every ``@deepseek-ai/dsh`` or
``@deepseek-ai/dsh-*`` peer must match the running runtime. Exact-pair
exemptions suppress the mismatch without hiding it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mth_components.plugin_manager.semver import is_valid, satisfies


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def unmet_requirements(
    manifest: Mapping[str, Any], runtime_version: str
) -> dict[str, str]:
    """Return the unmet requirement ranges, or an empty mapping."""
    if not is_valid(runtime_version):
        raise ValueError(f"Invalid dsh runtime version: {runtime_version}")
    engines = _object(manifest.get("engines", {}), "Plugin manifest engines")
    if "dsh" in engines:
        range_spec = engines["dsh"]
        if not isinstance(range_spec, str):
            raise ValueError("Plugin manifest engines.dsh must be a string")
        return (
            {}
            if satisfies(runtime_version, range_spec)
            else {"engines.dsh": range_spec}
        )
    if "peerDependencies" not in manifest:
        return {}
    peers = _object(manifest["peerDependencies"], "Plugin manifest peerDependencies")
    unmet: dict[str, str] = {}
    for name, range_spec in peers.items():
        if not isinstance(range_spec, str):
            raise ValueError(
                f"Plugin manifest peerDependencies[{name!r}] must be a string"
            )
        if name != "@deepseek-ai/dsh" and not name.startswith("@deepseek-ai/dsh-"):
            continue
        requirement = (
            runtime_version
            if range_spec in ("workspace:^", "workspace:~", "workspace:*")
            else range_spec
        )
        if not satisfies(runtime_version, requirement):
            unmet[name] = range_spec
    return unmet


def evaluate_plugin_compatibility(
    manifest: Mapping[str, Any],
    exemptions: Mapping[str, Any] | None = None,
    runtime_version: str = "0.3.0",
) -> dict[str, Any] | None:
    """Return structured compatibility facts when requirements are unmet."""
    requirements = unmet_requirements(manifest, runtime_version)
    if not requirements:
        return None
    name, version = manifest.get("name"), manifest.get("version")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Plugin manifest name must be a non-empty string")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("Plugin manifest version must be a non-empty string")
    allowed = (exemptions or {}).get(f"{name}@{version}", [])
    return {
        "name": name,
        "version": version,
        "runtimeVersion": runtime_version,
        "requirements": requirements,
        "exempted": isinstance(allowed, list) and runtime_version in allowed,
    }


__all__ = ["evaluate_plugin_compatibility", "unmet_requirements"]
