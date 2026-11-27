"""Core profile plugin management service.

Ports the core of ``packages/boot/plugin-manager/src/index.ts``: one manager
per profile over a durable manifest, plugin patches and version exemptions.
Bundles are installed from absolute local paths or git repositories; their
manifest ``dsh.bundle`` declares the rows they contribute.

The pinned GitHub connection checks, pnpm registry fallback, build-approval
dialogs, HMR reload/overridden detection, remote protocol, bundle detail
fetching, version listings and update/cancellation flows are NOT mounted here
(see PARITY row 2). They are distinct capabilities that depend on DSH-only
services absent from this harness.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from animas import Context, Service
from magitech_harness import __version__

from mth_components.plugin_manager.install_spec import (
    dependency_spec,
    parse_install_spec,
)
from mth_components.plugin_manager.registry import (
    NPMMIRROR_REGISTRY,
    normalize_registry,
)
from mth_components.plugin_manager.types import (
    ChangeResultDict,
    InstallCancelledError,
    InvalidInstallSpecError,
    ManagementFailure,
    PluginManagerError,
    management_error,
    read_profile_state,
)

DEFAULT_TIMEOUT_MS = 5_000
RUNTIME_VERSION = __version__
EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
PACKAGE_NAME = re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$")

MANIFEST_FILE = "package.json"
PATCHES_FILE = "plugin-patches.json"
COMPATIBILITY_FILE = "compatibility.json"
INSTALLED_DIR = "installed"

INSTALL_STATE = "plugin-manager/install-state"


class PluginManager(Service):
    """Durable profile plugin/bundle registry with stable change results."""

    def __init__(self, ctx: Context, config: Mapping[str, Any] | None = None) -> None:
        if ctx.get("pluginManager", None, strict=False) is not None:
            raise RuntimeError("plugin manager already registered")
        cfg = dict(config or {})
        root = cfg.get("root")
        if not isinstance(root, str) or not root:
            raise ValueError("plugin-manager: root must be a non-empty directory path")
        super().__init__(ctx, "pluginManager")
        self.root = Path(root).resolve()
        self._timeout_ms = int(cfg.get("timeoutMs", DEFAULT_TIMEOUT_MS))
        self._runtime_version = str(cfg.get("runtimeVersion", RUNTIME_VERSION))
        configured_registry = (
            None
            if cfg.get("registry") is None
            else normalize_registry(str(cfg["registry"]))
        )
        fallbacks = cfg.get("fallbackRegistries", [NPMMIRROR_REGISTRY])
        self._configured = {
            "registry": configured_registry,
            "fallbackRegistries": [normalize_registry(str(item)) for item in fallbacks],
        }
        self._official = frozenset(
            str(item) for item in (cfg.get("officialBundles") or [])
        )
        self._protected = frozenset(
            str(item) for item in (cfg.get("protectedPlugins") or [])
        )
        self._owner_entry_id = (
            str(cfg["ownerEntryId"]) if "ownerEntryId" in cfg else None
        )
        self._disposed = False

        def factory() -> Callable[[], None]:
            return self._dispose

        ctx.effect(factory, "plugin-manager: package cancellation")

    def _dispose(self) -> None:
        self._disposed = True

    def _path(self, name: str) -> Path:
        return self.root / name

    def _read_json(self, name: str, default: dict[str, Any]) -> dict[str, Any]:
        path = self._path(name)
        try:
            if not path.exists():
                return dict(default)
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise PluginManagerError(
                f"plugin-manager: failed to read the profile: {error}",
                "PLUGIN_MANAGER_PROFILE_READ_FAILED",
                error,
            ) from error
        if not isinstance(data, dict):
            raise PluginManagerError(
                "plugin-manager: failed to read the profile: expected an object",
                "PLUGIN_MANAGER_PROFILE_READ_FAILED",
            )
        return cast(dict[str, Any], data)

    def _write_json(self, name: str, value: Mapping[str, Any]) -> None:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(f"{path.suffix}.tmp")
        temp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)

    def _manifest(self) -> dict[str, Any]:
        data = self._read_json(
            MANIFEST_FILE,
            {"dependencies": {}, "dsh": {"profile": {"bundles": []}}},
        )
        data.setdefault("dependencies", {})
        data.setdefault("dsh", {}).setdefault("profile", {}).setdefault("bundles", [])
        return data

    def _patches(self) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._read_json(PATCHES_FILE, {"entries": {}}).setdefault("entries", {}),
        )

    def _compatibility(self) -> tuple[dict[str, list[str]], list[str], bool]:
        """Read the flat package@version map with warnings and rewritability."""
        path = self._path(COMPATIBILITY_FILE)
        if not path.exists():
            return {}, [], True
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as error:
            return {}, [f"{path} cannot be read ({error})"], False
        except ValueError as error:
            return {}, [f"{path} is not valid JSON ({error})"], False
        if not isinstance(raw, dict):
            return {}, [f"{path} must map package@version keys to version lists"], False
        exemptions: dict[str, list[str]] = {}
        warnings: list[str] = []
        for key, versions in raw.items():
            separator = key.rfind("@") if isinstance(key, str) else -1
            if (
                separator <= 0
                or PACKAGE_NAME.fullmatch(key[:separator]) is None
                or EXACT_VERSION.fullmatch(key[separator + 1 :]) is None
            ):
                warnings.append(
                    f"{path}: {key!r} is not an exact package-name@version key"
                )
                continue
            if not isinstance(versions, list) or any(
                not isinstance(item, str) or EXACT_VERSION.fullmatch(item) is None
                for item in versions
            ):
                warnings.append(
                    f"{path}: {key} must contain a list of exact DSH versions"
                )
                continue
            exemptions[key] = list(versions)
        return exemptions, warnings, not warnings

    def _package_manifest(self, name: str) -> dict[str, Any] | None:
        path = self._path(INSTALLED_DIR) / name / MANIFEST_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _disk_state(self) -> str:
        """The persisted files whose differences mark a real change."""
        parts: list[str] = []
        for name in (
            MANIFEST_FILE,
            PATCHES_FILE,
            "cordis.patch.yml",
            "pnpm-workspace.yaml",
            COMPATIBILITY_FILE,
        ):
            path = self._path(name)
            try:
                parts.append(path.read_text(encoding="utf-8") if path.exists() else "")
            except OSError:
                parts.append("")
        return "\0".join(parts)

    def _change(
        self,
        stage: str,
        target: str,
        step: Callable[[ChangeResultDict], str | None],
        *,
        enabled: bool | None = None,
    ) -> ChangeResultDict:
        result: ChangeResultDict = {
            "changed": False,
            "application": "applied",
            "stage": cast(Any, stage),
            "target": target,
        }
        if enabled is not None:
            result["enabled"] = enabled
        before = self._disk_state()
        try:
            step(result)
            result["changed"] = before != self._disk_state()
        except InstallCancelledError:
            result["application"] = "cancelled"
        except (
            ManagementFailure,
            PluginManagerError,
            InvalidInstallSpecError,
            OSError,
            ValueError,
        ) as error:
            result["application"] = "failed"
            result["error"] = management_error(error)
        return result

    def _guard(self) -> None:
        if self._disposed:
            raise PluginManagerError(
                "plugin-manager: the manager was disposed",
                "PLUGIN_MANAGER_UNAVAILABLE",
            )

    async def list_plugins(self) -> list[dict[str, Any]]:
        """Current plugins with their persistent patch targets."""
        self._guard()
        rows: dict[str, Any] = read_profile_state(self._patches)
        plugins: list[dict[str, Any]] = []
        for entry_id, entry in rows.items():
            item: dict[str, Any] = {
                "entryId": entry_id,
                "moduleName": str(entry.get("moduleName", "")),
                "enabled": bool(entry.get("enabled", True)),
            }
            module = item["moduleName"]
            if module in self._protected or entry_id == self._owner_entry_id:
                item["readOnlyReason"] = "management-required"
            else:
                item["patchId"] = entry_id
            plugins.append(item)
        return plugins

    async def list_bundles(self) -> list[dict[str, Any]]:
        """Installed, selected and official bundles in the current profile."""
        self._guard()
        manifest: dict[str, Any] = read_profile_state(self._manifest)
        recorded = cast(dict[str, str], manifest["dependencies"])
        selected = cast(list[str], manifest["dsh"]["profile"]["bundles"])
        names = sorted({*selected, *recorded, *self._official})
        bundles: list[dict[str, Any]] = []
        for name in names:
            installed = name in recorded
            enabled = name in selected
            info = self._package_manifest(name)
            version = info.get("version") if info else None
            bundle: dict[str, Any] = {
                "name": name,
                "official": name in self._official,
                "availability": "profile" if info else "missing",
                "enabled": enabled,
                "installed": installed,
                "optional": False,
                "removable": installed
                and name not in self._official
                and name not in self._protected,
                "rows": [],
                "overrides": [],
            }
            if installed:
                bundle["source"] = dependency_spec(name, recorded[name], str(self.root))
            if isinstance(version, str):
                bundle["version"] = version
            description = info.get("description") if info else None
            if isinstance(description, str) and description:
                bundle["description"] = description
            if info and isinstance(info.get("dsh"), dict) and "bundle" in info["dsh"]:
                bundle["rows"] = [
                    {
                        "rowId": str(row.get("id", "")),
                        "moduleName": str(row.get("module", "")),
                    }
                    for row in (info["dsh"]["bundle"].get("rows") or [])
                    if isinstance(row, dict)
                ]
            elif info:
                bundle["error"] = {"code": "not-bundle"}
            if name in self._protected:
                bundle["readOnlyReason"] = "management-required"
            bundles.append(bundle)
        return bundles

    async def registries(self) -> dict[str, Any]:
        """Configured registries; ``resolved`` stays null without pnpm."""
        self._guard()
        return {
            **self._configured,
            "fallbackRegistries": list(
                cast(list[str], self._configured["fallbackRegistries"])
            ),
            "resolved": None,
        }

    def inspect(self, spec: str) -> dict[str, Any]:
        """Read a spec before anything is installed."""
        self._guard()
        try:
            parsed = parse_install_spec(spec)
        except InvalidInstallSpecError as error:
            return {
                "status": "refused",
                "problem": "invalid-spec",
                "reason": error.reason,
            }
        manifest: dict[str, Any] | None = None
        if parsed["kind"] == "path":
            try:
                manifest = json.loads(
                    (Path(parsed["path"]) / MANIFEST_FILE).read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                manifest = None
        if manifest is None:
            return {
                "status": "accepted",
                "kind": parsed["kind"],
                "bundle": False,
                "registry": None,
            }
        declared = manifest.get("dsh")
        is_bundle = isinstance(declared, dict) and isinstance(
            declared.get("bundle"), dict
        )
        answer: dict[str, Any] = {
            "status": "accepted",
            "kind": parsed["kind"],
            "bundle": is_bundle,
            "registry": None,
        }
        for field in ("name", "version", "description"):
            value = manifest.get(field)
            if isinstance(value, str) and value:
                answer[field] = value
        return answer

    def list_version_exemptions(self) -> dict[str, Any]:
        """Read exact plugin/runtime exemptions from the profile."""
        self._guard()
        exemptions, warnings, _rewritable = read_profile_state(self._compatibility)
        return {"exemptions": dict(exemptions), "warnings": warnings}

    def set_version_exemption(
        self,
        package_version: str,
        runtime_version: str,
        enabled: bool,
        accept_risk: bool = False,
    ) -> ChangeResultDict:
        """Grant or revoke one exact plugin/runtime compatibility exemption."""
        self._guard()

        def step(result: ChangeResultDict) -> None:
            separator = package_version.rfind("@")
            if (
                separator <= 0
                or PACKAGE_NAME.fullmatch(package_version[:separator]) is None
                or EXACT_VERSION.fullmatch(package_version[separator + 1 :]) is None
                or EXACT_VERSION.fullmatch(runtime_version) is None
            ):
                raise ManagementFailure(
                    "invalid-spec",
                    "version exemptions require an exact package@version and an "
                    "exact DSH runtime version",
                )
            if enabled and not accept_risk:
                raise ManagementFailure(
                    "operation-error", "acceptRisk is required for a grant"
                )
            if enabled and runtime_version != self._runtime_version:
                raise ManagementFailure(
                    "operation-error",
                    f"this application runs DSH {self._runtime_version}",
                )
            exemptions, warnings, rewritable = self._compatibility()
            if not rewritable:
                raise ManagementFailure(
                    "operation-error",
                    "compatibility file must be repaired before changes",
                )
            versions = set(exemptions.get(package_version, []))
            if enabled:
                versions.add(runtime_version)
            else:
                versions.discard(runtime_version)
            if versions:
                exemptions[package_version] = sorted(versions)
            else:
                exemptions.pop(package_version, None)
            self._write_json(COMPATIBILITY_FILE, exemptions)
            result["warnings"] = warnings
            result["application"] = "restart-required"
            result["restartReason"] = "no-hmr"

        return self._change("enable", package_version, step, enabled=enabled)

    def set_plugin_enabled(self, entry_id: str, enabled: bool) -> ChangeResultDict:
        """Persist one plugin row's enabled state."""
        self._guard()

        def step(result: ChangeResultDict) -> None:
            patches = self._patches()
            entry = patches.get(entry_id)
            if not isinstance(entry, dict):
                raise ManagementFailure("unknown-plugin")
            if (
                entry.get("moduleName") in self._protected
                or entry_id == self._owner_entry_id
            ):
                raise ManagementFailure("management-required")
            entry["enabled"] = bool(enabled)
            patches[entry_id] = entry
            self._write_json(PATCHES_FILE, {"entries": patches})
            result["application"] = "restart-required"
            result["restartReason"] = "no-hmr"

        return self._change("enable", entry_id, step, enabled=enabled)

    def set_bundle_enabled(self, name: str, enabled: bool) -> ChangeResultDict:
        """Select or unselect a bundle layer while keeping its files."""
        self._guard()

        def step(result: ChangeResultDict) -> None:
            manifest = self._manifest()
            selected = cast(list[str], manifest["dsh"]["profile"]["bundles"])
            if enabled and name in selected:
                return
            if not enabled and name not in selected:
                return
            if enabled and name not in self._package_names():
                raise ManagementFailure("unknown-plugin")
            if name in self._protected:
                raise ManagementFailure("management-required")
            if name in self._official and not enabled:
                raise ManagementFailure("not-removable")
            updated = [item for item in selected if item != name]
            if enabled:
                updated.append(name)
            manifest["dsh"]["profile"]["bundles"] = updated
            self._write_json(MANIFEST_FILE, manifest)
            result["application"] = "restart-required"
            result["restartReason"] = "no-hmr"

        return self._change("enable", name, step, enabled=enabled)

    def _package_names(self) -> set[str]:
        return (
            {
                item.name
                for item in (self._path(INSTALLED_DIR)).iterdir()
                if item.is_dir()
            }
            if (self._path(INSTALLED_DIR)).exists()
            else set()
        )

    def install_bundle(  # noqa: PLR0915
        self, spec: str, options: Mapping[str, Any] | None = None
    ) -> ChangeResultDict:
        """Install a bundle from an absolute path or git repository."""
        self._guard()
        opts = dict(options or {})
        enabled = bool(opts.get("enabled", True))

        def step(result: ChangeResultDict) -> None:
            if not spec.strip() or spec.startswith("-"):
                raise ManagementFailure("invalid-spec")
            parsed = parse_install_spec(spec)
            if parsed["kind"] not in ("path", "git"):
                raise ManagementFailure(
                    "operation-error",
                    "plugin-manager core installs local paths and git repositories; "
                    "registry and tarball installs require the pnpm package operation",
                )
            source = (
                Path(cast(str, parsed["path"])) if parsed["kind"] == "path" else None
            )
            if source is not None and not source.is_dir():
                raise ManagementFailure(
                    "invalid-spec", "local bundle path is not a directory"
                )
            current = self._manifest()
            recorded = cast(dict[str, str], current["dependencies"])
            staged = self._path(INSTALLED_DIR) / ".staging"
            if staged.exists():
                shutil.rmtree(staged)
            self._materialize(parsed, source, staged)
            manifest = self._package_manifest_from(staged, parsed)
            if manifest is None:
                shutil.rmtree(staged, ignore_errors=True)
                raise ManagementFailure("not-bundle", "no readable package.json")
            name = manifest.get("name")
            version = manifest.get("version")
            if not isinstance(name, str) or not name:
                shutil.rmtree(staged, ignore_errors=True)
                raise ManagementFailure("not-bundle", "package.json has no name")
            declared = manifest.get("dsh")
            if not isinstance(declared, dict) or not isinstance(
                declared.get("bundle"), dict
            ):
                shutil.rmtree(staged, ignore_errors=True)
                raise ManagementFailure("not-bundle")
            if name in recorded:
                shutil.rmtree(staged, ignore_errors=True)
                raise ManagementFailure("already-installed")
            target = self._path(INSTALLED_DIR) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target)
            staged.rename(target)
            recorded[name] = (
                str(parsed["spec"]) if parsed["kind"] == "git" else f"file:{source}"
            )
            current["dependencies"] = recorded
            if enabled:
                selected = cast(list[str], current["dsh"]["profile"]["bundles"])
                if name not in selected:
                    selected.append(name)
            self._write_json(MANIFEST_FILE, current)
            self._sync_patches(name, manifest, enabled)
            result["bundle"] = name
            if isinstance(version, str):
                result["version"] = version
            result["application"] = "restart-required"
            result["restartReason"] = "no-hmr"
            self.ctx.emit(
                INSTALL_STATE,
                {"target": name, "phase": "applied", "enabled": enabled},
            )

        return self._change("install", spec, step, enabled=enabled)

    @staticmethod
    def _package_manifest_from(
        source: Path, _parsed: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        try:
            data = json.loads((source / MANIFEST_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _materialize(
        self, parsed: Mapping[str, Any], source: Path | None, target: Path
    ) -> None:
        if source is not None:
            shutil.copytree(
                source,
                target,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules"),
            )
            return
        location = cast(str, parsed["spec"]).split("#", 1)[0].replace("git+", "")
        commit = None
        for part in cast(str, parsed["spec"]).split("#", 1)[1].split("&"):
            if "path:" not in part:
                commit = part
        if commit is None:
            raise ManagementFailure("invalid-spec", "a git spec must pin a commit")
        result = subprocess.run(
            ["git", "clone", "--no-checkout", "--", location, str(target)],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=self._timeout_ms / 1000,
            check=False,
        )
        if result.returncode != 0:
            raise ManagementFailure(
                "operation-error", result.stderr.strip() or "git clone failed"
            )
        checkout = subprocess.run(
            ["git", "-C", str(target), "checkout", "--detach", commit],
            capture_output=True,
            text=True,
            timeout=self._timeout_ms / 1000,
            check=False,
        )
        if checkout.returncode != 0:
            shutil.rmtree(target, ignore_errors=True)
            raise ManagementFailure("invalid-spec", "git commit is unavailable")

    def _sync_patches(
        self, name: str, manifest: Mapping[str, Any], enabled: bool
    ) -> None:
        patches = self._patches()
        declared = manifest.get("dsh", {}).get("bundle", {})
        rows = declared.get("rows", []) if isinstance(declared, dict) else []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            row_id = str(row.get("id", ""))
            if not row_id:
                continue
            patches[row_id] = {
                "moduleName": str(row.get("module", "")),
                "bundle": name,
                "enabled": enabled,
            }
        self._write_json(PATCHES_FILE, {"entries": patches})

    def remove_bundle(self, name: str) -> ChangeResultDict:
        """Remove an installed third-party bundle and its selection."""
        self._guard()

        def step(result: ChangeResultDict) -> None:
            manifest = self._manifest()
            recorded = cast(dict[str, str], manifest["dependencies"])
            selected = cast(list[str], manifest["dsh"]["profile"]["bundles"])
            if name not in recorded:
                if name not in selected:
                    raise ManagementFailure("not-removable")
                manifest["dsh"]["profile"]["bundles"] = [
                    item for item in selected if item != name
                ]
                self._write_json(MANIFEST_FILE, manifest)
                return
            if name in self._official or name in self._protected:
                raise ManagementFailure("not-removable")
            if any(
                isinstance(item, dict)
                and item.get("bundle") == name
                and item.get("enabled")
                for item in self._patches().values()
            ):
                raise ManagementFailure("bundle-in-use")
            manifest["dependencies"] = {
                key: value for key, value in recorded.items() if key != name
            }
            manifest["dsh"]["profile"]["bundles"] = [
                item for item in selected if item != name
            ]
            self._write_json(MANIFEST_FILE, manifest)
            target = self._path(INSTALLED_DIR) / name
            if target.exists():
                shutil.rmtree(target)
            patches = {
                key: value
                for key, value in self._patches().items()
                if not (isinstance(value, dict) and value.get("bundle") == name)
            }
            self._write_json(PATCHES_FILE, {"entries": patches})
            result["application"] = "restart-required"
            result["restartReason"] = "no-hmr"

        return self._change("remove", name, step)


__all__ = [
    "COMPATIBILITY_FILE",
    "INSTALLED_DIR",
    "INSTALL_STATE",
    "MANIFEST_FILE",
    "PATCHES_FILE",
    "RUNTIME_VERSION",
    "PluginManager",
]
