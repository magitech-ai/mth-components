"""Install target for an official bundle.

Ports ``packages/boot/plugin-manager/src/official-install-target.ts``: a
published bundle resolves to ``<name>@<version>``; a source checkout resolves
only when exactly one workspace package is built and complete.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def official_bundle_install_target(
    name: str, install_anchor: str, version: str
) -> dict[str, str]:
    """Return the verified install spec and exact version for a bundle."""
    anchor = Path(install_anchor).resolve()
    installation = _read(anchor)
    dependencies = installation.get("dependencies") if installation else None
    if (
        installation is None
        or installation.get("name") != "@deepseek-ai/dsh"
        or not isinstance(dependencies, dict)
        or not any(
            str(value).startswith("workspace:") for value in dependencies.values()
        )
    ):
        return {"spec": f"{name}@{version}", "version": version}

    if (
        anchor.name != "package.json"
        or anchor.parent.name != "cli"
        or anchor.parent.parent.name != "apps"
    ):
        raise ValueError(
            "official bundle install anchor must be <root>/apps/cli/package.json"
        )
    root = anchor.parent.parent.parent
    root_manifest = _read(root / "package.json")
    if root_manifest is None or root_manifest.get("name") != "@deepseek-ai/dsh-root":
        raise ValueError("official bundle source checkout has no root manifest")

    candidates: list[Path] = []
    for package in sorted(Path(root, "packages").glob("*/*/package.json")):
        manifest = _read(package)
        if manifest is not None and manifest.get("name") == name:
            candidates.append(package.parent)
    if len(candidates) != 1:
        raise ValueError("official bundle source checkout has no unique package")
    bundle_dir = candidates[0]
    manifest = _read(bundle_dir / "package.json") or {}
    if manifest.get("version") != version or manifest.get("private") is True:
        raise ValueError("official bundle package does not match the runtime version")
    dsh = manifest.get("dsh")
    if not isinstance(dsh, dict) or not isinstance(dsh.get("bundle"), dict):
        raise ValueError("official bundle package declares no bundle")
    main = manifest.get("main")
    if not isinstance(main, str) or not (bundle_dir / main).is_file():
        raise ValueError("official bundle package is not built")
    for patch in dsh["bundle"].get("patch", []) or []:
        if isinstance(patch, str) and not (bundle_dir / patch).is_file():
            raise ValueError("official bundle patch file is missing")
    return {"spec": f"link:{bundle_dir.resolve()}", "version": version}


__all__ = ["official_bundle_install_target"]
