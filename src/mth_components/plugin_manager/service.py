"""Profile plugin/bundle management service.

Ports ``packages/boot/plugin-manager/src/index.ts``: durable profile state,
bundle admission through pnpm or a local/git source, build approval, registry
fallback, version lookups, updates, cancellation and operation diagnostics.
A mounted HMR service is used for exclusive mutation and reconciliation;
without one, changes report ``restart-required/no-hmr`` as pinned.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from animas import Context, Service
from magitech_harness import __version__

from mth_components.plugin_manager import (
    build_approval,
    bundle_detail,
    compatibility,
    diagnostic,
    git_transport,
    github_connection,
    install_failure,
    install_spec,
    official_install_target,
    operations,
    versions,
)
from mth_components.plugin_manager import (
    catalog as catalog_module,
)
from mth_components.plugin_manager.install_spec import (
    dependency_spec,
    parse_install_spec,
    pinned_git_origin,
)
from mth_components.plugin_manager.registry import (
    NPMMIRROR_REGISTRY,
    attribute_failure,
    normalize_registry,
    registry_plan,
)
from mth_components.plugin_manager.types import (
    ChangeResultDict,
    InstallCancelledError,
    InvalidInstallSpecError,
    ManagementFailure,
    PluginManagerError,
    RemoteError,
    management_error,
    read_profile_state,
)

DEFAULT_TIMEOUT_MS = 5_000
DEFAULT_OUTPUT_BYTES = 16_384
DEFAULT_IDLE_TIMEOUT_MS = 600_000
DEFAULT_INSPECT_TIMEOUT_MS = 20_000
DEFAULT_VERSION_LIST_LIMIT = 20
DEFAULT_VERSION_LOOKUP_CONCURRENCY = 4
RUNTIME_VERSION = __version__
EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
PACKAGE_NAME = re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$")

MANIFEST_FILE = "package.json"
PATCHES_FILE = "plugin-patches.json"
COMPATIBILITY_FILE = "compatibility.json"
INSTALLED_DIR = "installed"
WORKSPACE_FILE = "pnpm-workspace.yaml"

INSTALL_STATE = "plugin-manager/install-state"
INSTALL_LOG = "plugin-manager/install-log"
OPERATION_EVENT = "plugin-manager/operation"
CHANGED_EVENT = "plugin-manager/changed"
UPDATE_STATE = "plugin-manager/update-state"


def _positive_int(config: Mapping[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"plugin-manager: {key} must be a positive integer")
    return value


class _AbortState:
    """Cancellation view over an installation control record."""

    def __init__(self, control: Mapping[str, Any]) -> None:
        self._control = control

    @property
    def aborted(self) -> bool:
        return bool(self._control.get("aborted", False))

    def throw_if_aborted(self) -> None:
        if self.aborted:
            raise InstallCancelledError()


def _restore_snapshot(root: Path) -> None:
    """Restore the two files an installation may rewrite."""
    for name in ("package.json", "pnpm-lock.yaml"):
        temporary = root / f".{name}.snapshot"
        target = root / name
        if temporary.exists():
            temporary.replace(target)


def _capture_install(root: Path) -> dict[str, bytes | None]:
    """Remember the profile files an installation may rewrite."""
    captured: dict[str, bytes | None] = {}
    for name in ("package.json", PATCHES_FILE, "pnpm-lock.yaml"):
        path = root / name
        captured[name] = path.read_bytes() if path.exists() else None
    return captured


def _restore_capture(root: Path, captured: Mapping[str, bytes | None]) -> None:
    """Put captured profile files back, removing ones that did not exist."""
    for name, data in captured.items():
        path = root / name
        if data is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(data)


class PluginManager(Service):
    """Durable profile plugin and bundle registry with stable change results."""

    def __init__(self, ctx: Context, config: Mapping[str, Any] | None = None) -> None:
        if ctx.get("pluginManager", None, strict=False) is not None:
            raise RuntimeError("plugin manager already registered")
        cfg = dict(config or {})
        root = cfg.get("root")
        if not isinstance(root, str) or not root:
            raise ValueError("plugin-manager: root must be a non-empty directory path")
        super().__init__(ctx, "pluginManager")
        self.root = Path(root).resolve()
        self._timeout_ms = _positive_int(cfg, "timeoutMs", DEFAULT_TIMEOUT_MS)
        self._output_bytes = _positive_int(cfg, "outputBytes", DEFAULT_OUTPUT_BYTES)
        self._idle_timeout_ms = _positive_int(
            cfg, "idleTimeoutMs", DEFAULT_IDLE_TIMEOUT_MS
        )
        self._inspect_timeout_ms = _positive_int(
            cfg, "inspectTimeoutMs", DEFAULT_INSPECT_TIMEOUT_MS
        )
        self._github_timeout_ms = _positive_int(
            cfg, "githubConnectionTimeoutMs", DEFAULT_TIMEOUT_MS
        )
        self._git_timeout_ms = _positive_int(
            cfg, "gitTransportTimeoutMs", DEFAULT_TIMEOUT_MS
        )
        self._version_list_limit = _positive_int(
            cfg, "versionListLimit", DEFAULT_VERSION_LIST_LIMIT
        )
        self._version_lookup_concurrency = _positive_int(
            cfg, "versionLookupConcurrency", DEFAULT_VERSION_LOOKUP_CONCURRENCY
        )
        self._document_max_bytes = _positive_int(
            cfg, "documentMaxBytes", bundle_detail.DEFAULT_DOCUMENT_BYTES
        )
        self._image_max_bytes = _positive_int(
            cfg, "imageMaxBytes", bundle_detail.DEFAULT_IMAGE_BYTES
        )
        self._cancel_poll_ms = _positive_int(
            cfg, "cancelPollMs", operations.DEFAULT_CANCEL_POLL_MS
        )
        self._command = str(cfg.get("pnpmCommand", "pnpm"))
        self._install_anchor = str(cfg.get("installAnchor", self.root / MANIFEST_FILE))
        self._runtime_version = str(cfg.get("runtimeVersion", RUNTIME_VERSION))
        configured_registry = (
            None
            if cfg.get("registry") is None
            else normalize_registry(str(cfg["registry"]))
        )
        fallbacks = cfg.get("fallbackRegistries", [NPMMIRROR_REGISTRY])
        self._configured: dict[str, Any] = {
            "registry": configured_registry,
            "fallbackRegistries": [normalize_registry(str(item)) for item in fallbacks],
            "resolved": None,
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
        self._management_bundles: set[str] = {
            str(item) for item in (cfg.get("startedBundles") or [])
        }
        self._npm_sources: list[str] = [
            str(item) for item in (cfg.get("npmFileSources") or [])
        ]
        self._installed_from: dict[str, str | None] = {}
        self._installs: dict[str, dict[str, Any]] = {}
        self._disposed = False
        self._hmr = ctx.get("hmr", None, strict=False)

        def factory() -> Callable[[], None]:
            return self._dispose

        ctx.effect(factory, "plugin-manager: package cancellation")

    def _dispose(self) -> None:
        self._disposed = True

    def _guard(self) -> None:
        if self._disposed:
            raise PluginManagerError(
                "plugin-manager: the manager was disposed",
                "PLUGIN_MANAGER_UNAVAILABLE",
            )

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
        for key, value_list in raw.items():
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
            if not isinstance(value_list, list) or any(
                not isinstance(item, str) or EXACT_VERSION.fullmatch(item) is None
                for item in value_list
            ):
                warnings.append(
                    f"{path}: {key} must contain a list of exact DSH versions"
                )
                continue
            exemptions[key] = list(value_list)
        return exemptions, warnings, not warnings

    def _package_manifest(self, name: str) -> dict[str, Any] | None:
        return self._read_manifest(self._path(INSTALLED_DIR) / name)

    @staticmethod
    def _read_manifest(directory: Path) -> dict[str, Any] | None:
        try:
            data = json.loads((directory / MANIFEST_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _disk_state(self) -> str:
        parts: list[str] = []
        for name in (
            MANIFEST_FILE,
            PATCHES_FILE,
            "cordis.patch.yml",
            WORKSPACE_FILE,
            COMPATIBILITY_FILE,
        ):
            path = self._path(name)
            try:
                parts.append(path.read_text(encoding="utf-8") if path.exists() else "")
            except OSError:
                parts.append("")
        return "\0".join(parts)

    def _emit_operation(
        self, phase: str, result: Mapping[str, Any], reason: str = "bundle"
    ) -> None:
        payload: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "time": datetime.now(UTC).isoformat(),
            "reason": reason,
            "target": result.get("target"),
            "stage": result.get("stage", "install"),
            "phase": phase,
        }
        if result.get("enabled") is not None:
            payload["enabled"] = result["enabled"]
        if result.get("version") is not None:
            payload["version"] = result["version"]
        if phase == "settled":
            payload["application"] = result.get("application")
            payload["changed"] = bool(result.get("changed"))
            error = result.get("error") or {}
            if error.get("code") is not None:
                payload["errorCode"] = error["code"]
            if result.get("restartReason") is not None:
                payload["restartReason"] = result["restartReason"]
        validated = diagnostic.parse_operation_record(
            {
                "runId": payload["id"],
                "operation": payload,
            }
        )
        if validated is not None:
            self.ctx.emit(OPERATION_EVENT, validated.operation)

    async def _change(
        self,
        stage: str,
        target: str,
        step: Callable[[ChangeResultDict], None],
        *,
        enabled: bool | None = None,
        reason: str = "bundle",
    ) -> ChangeResultDict:
        result: ChangeResultDict = {
            "changed": False,
            "application": "applied",
            "stage": cast(Any, stage),
            "target": target,
        }
        if enabled is not None:
            result["enabled"] = enabled
        self._emit_operation("started", result, reason)
        before = self._disk_state()

        def apply() -> None:
            try:
                step(result)
                result["changed"] = before != self._disk_state()
            except InstallCancelledError:
                result["application"] = "cancelled"
            except (
                ManagementFailure,
                PluginManagerError,
                InvalidInstallSpecError,
                RemoteError,
                OSError,
                ValueError,
            ) as error:
                result["application"] = "failed"
                result["error"] = management_error(error)

        await self._configure(apply)
        self.ctx.emit(CHANGED_EVENT, {"changed": result["changed"], "target": target})
        self._emit_operation("settled", result, reason)
        return result

    async def _configure(self, job: Callable[[], Any]) -> Any:
        self._guard()
        hmr = self._hmr
        if hmr is not None and hasattr(hmr, "run_exclusive"):
            return await hmr.run_exclusive(job)
        answer = job()
        return await answer if inspect.isawaitable(answer) else answer

    async def list_plugins(self) -> list[dict[str, Any]]:
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
            if (
                module in self._protected
                or module in self._management_bundles
                or entry_id == self._owner_entry_id
            ):
                item["readOnlyReason"] = "management-required"
            else:
                item["patchId"] = entry_id
            plugins.append(item)
        return plugins

    async def list_bundles(self) -> list[dict[str, Any]]:
        self._guard()
        manifest = read_profile_state(self._manifest)
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
            if name in self._official:
                try:
                    bundle["installTarget"] = dict(
                        official_install_target.official_bundle_install_target(
                            name, self._install_anchor, self._runtime_version
                        )
                    )
                except (ValueError, OSError):
                    pass
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
            if name in self._protected or name in self._management_bundles:
                bundle["readOnlyReason"] = "management-required"
            bundles.append(bundle)
        return bundles

    async def registries(self) -> dict[str, Any]:
        self._guard()
        result = await operations.read_profile_registry(
            str(self.root), self._command, self._timeout_ms
        )
        self._configured["resolved"] = result
        return {
            **self._configured,
            "fallbackRegistries": list(self._configured["fallbackRegistries"]),
        }

    def inspect(self, spec: str) -> dict[str, Any]:
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
            manifest = self._read_manifest(Path(cast(str, parsed["path"])))
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
        self._guard()
        exemptions, warnings, _rewritable = read_profile_state(self._compatibility)
        return {"exemptions": dict(exemptions), "warnings": warnings}

    async def set_version_exemption(
        self,
        package_version: str,
        runtime_version: str,
        enabled: bool,
        accept_risk: bool = False,
    ) -> ChangeResultDict:
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
            value_list = set(exemptions.get(package_version, []))
            if enabled:
                value_list.add(runtime_version)
            else:
                value_list.discard(runtime_version)
            if value_list:
                exemptions[package_version] = sorted(value_list)
            else:
                exemptions.pop(package_version, None)
            self._write_json(COMPATIBILITY_FILE, exemptions)
            result["warnings"] = warnings
            result["application"] = "restart-required"
            result["restartReason"] = "no-hmr"

        return await self._change("enable", package_version, step, enabled=enabled)

    async def set_plugin_enabled(
        self, entry_id: str, enabled: bool
    ) -> ChangeResultDict:
        self._guard()

        def step(result: ChangeResultDict) -> None:
            patches = self._patches()
            entry = patches.get(entry_id)
            if not isinstance(entry, dict):
                raise ManagementFailure("unknown-plugin")
            module = str(entry.get("moduleName", ""))
            if (
                module in self._protected
                or module in self._management_bundles
                or entry_id == self._owner_entry_id
            ):
                raise ManagementFailure("management-required")
            issue = compatibility.evaluate_plugin_compatibility(
                self._package_manifest(module) or {},
                self.list_version_exemptions()["exemptions"],
                self._runtime_version,
            )
            if issue is not None and not issue["exempted"]:
                raise ManagementFailure("incompatible-version", incompatible=[issue])
            entry["enabled"] = bool(enabled)
            patches[entry_id] = entry
            self._write_json(PATCHES_FILE, {"entries": patches})
            result["application"] = "restart-required"
            result["restartReason"] = "no-hmr"

        return await self._change("enable", entry_id, step, enabled=enabled)

    async def set_bundle_enabled(self, name: str, enabled: bool) -> ChangeResultDict:
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
            if name in self._protected or name in self._management_bundles:
                raise ManagementFailure("management-required")
            if name in self._official and not enabled:
                raise ManagementFailure("not-removable")
            if enabled:
                info = self._package_manifest(name) or {}
                issue = compatibility.evaluate_plugin_compatibility(
                    info,
                    self.list_version_exemptions()["exemptions"],
                    self._runtime_version,
                )
                if issue is not None and not issue["exempted"]:
                    raise ManagementFailure(
                        "incompatible-version", incompatible=[issue]
                    )
            updated = [item for item in selected if item != name]
            if enabled:
                updated.append(name)
            manifest["dsh"]["profile"]["bundles"] = updated
            self._write_json(MANIFEST_FILE, manifest)
            result["application"] = "restart-required"
            result["restartReason"] = "no-hmr"

        return await self._change("enable", name, step, enabled=enabled)

    def _package_names(self) -> set[str]:
        directory = self._path(INSTALLED_DIR)
        return (
            {
                item.name
                for item in directory.iterdir()
                if item.is_dir() and not item.name.startswith(".")
            }
            if directory.exists()
            else set()
        )

    async def install_bundle(  # noqa: PLR0912, PLR0915
        self, spec: str, options: Mapping[str, Any] | None = None
    ) -> ChangeResultDict:
        """Install one bundle through pnpm, a local path or a git source."""
        self._guard()
        opts = dict(options or {})
        request_id = str(opts["requestId"]) if "requestId" in opts else None
        control: dict[str, Any] = {
            "aborted": False,
            "phase": "installing",
            "signal": None,
            "beforeDependencies": operations.direct_dependencies(self._manifest()),
            "result": None,
        }
        if request_id is not None:
            control = self._installs.setdefault(request_id, control)
        control["signal"] = _AbortState(control)
        result: ChangeResultDict = {
            "changed": False,
            "application": "applied",
            "stage": "install",
            "target": spec,
        }
        if opts.get("enabled", True):
            result["enabled"] = True
        self._emit_operation("started", result, "install")
        before = self._disk_state()
        captured = _capture_install(self.root)
        installed_before = self._package_names()
        name: str | None = None
        version: str | None = None
        try:
            if not spec.strip() or spec.startswith("-"):
                raise ManagementFailure("invalid-spec")
            control["signal"].throw_if_aborted()
            parsed = parse_install_spec(spec)
            kind = cast(str, parsed["kind"])
            if opts.get("approvedBuilds"):
                build_approval.approve_builds(
                    str(self.root), list(opts["approvedBuilds"])
                )
                result["approvedBuilds"] = list(opts["approvedBuilds"])
            if kind == "git":
                connection = await github_connection.check_github_connection(
                    parsed,
                    str(self.root),
                    timeout_ms=self._github_timeout_ms,
                    output_bytes=self._output_bytes,
                )
                if connection is not None and connection["kind"] in (
                    "network",
                    "timeout",
                ):
                    result["packageResult"] = connection
                    result["failedAt"] = "spec-host"
                    self.ctx.emit(
                        INSTALL_LOG,
                        {
                            "requestId": request_id,
                            "argv": connection["argv"],
                            "text": connection["output"],
                        },
                    )
                    raise ManagementFailure("operation-error", connection["output"])
                selected_spec = await git_transport.choose_git_transport(
                    parsed,
                    str(self.root),
                    timeout_ms=self._git_timeout_ms,
                    signal=control["signal"],
                )
            else:
                selected_spec = str(parsed["spec"])

            if kind in ("registry", "tarball"):
                await self._install_with_pnpm(
                    selected_spec, parsed, result, opts, control, request_id
                )
                name, version = self._installed_from_spec(
                    result, opts, spec, parsed, control
                )
            else:
                name, version = await self._install_local_source(
                    parsed, result, bool(opts.get("enabled", True))
                )

            issue = compatibility.evaluate_plugin_compatibility(
                self._package_manifest(name) or {},
                self.list_version_exemptions()["exemptions"],
                self._runtime_version,
            )
            if issue is not None and not issue["exempted"]:
                raise ManagementFailure("incompatible-version", incompatible=[issue])
            control["phase"] = "applying"
            control["signal"].throw_if_aborted()
            result["bundle"] = name
            if version is not None:
                result["version"] = version
            result["stage"] = "enable"
            result["application"] = "restart-required"
            result["restartReason"] = "no-hmr"
            if opts.get("enabled", True):

                def apply() -> None:
                    current = self._manifest()
                    selected = cast(list[str], current["dsh"]["profile"]["bundles"])
                    if name not in selected:
                        selected.append(name)
                    self._write_json(MANIFEST_FILE, current)
                    contributed = [
                        item
                        for item in self._patches().values()
                        if isinstance(item, dict) and item.get("bundle") == name
                    ]
                    if contributed and not any(
                        item.get("enabled") is True for item in contributed
                    ):
                        result["application"] = "overridden"

                await self._configure(apply)
            self.ctx.emit(
                INSTALL_STATE,
                {"requestId": request_id, "target": name, "phase": "applying"},
            )
        except InstallCancelledError:
            result["application"] = "cancelled"
            if control["phase"] != "applying":
                self._rollback_install(captured, installed_before)
        except (
            ManagementFailure,
            PluginManagerError,
            InvalidInstallSpecError,
            RemoteError,
            OSError,
            ValueError,
        ) as error:
            result["application"] = "failed"
            result["error"] = management_error(error)
            if control["phase"] != "applying":
                self._rollback_install(captured, installed_before)
        finally:
            control["result"] = result
        result["changed"] = before != self._disk_state()
        if request_id is not None:
            self._installs.pop(request_id, None)
        self.ctx.emit(CHANGED_EVENT, {"changed": result["changed"], "target": spec})
        self._emit_operation("settled", result, "install")
        return result

    def _rollback_install(
        self, captured: Mapping[str, bytes | None], installed_before: set[str]
    ) -> None:
        """Undo profile and installed-package changes from a failed install."""
        for name in self._package_names() - installed_before:
            shutil.rmtree(self._path(INSTALLED_DIR) / name, ignore_errors=True)
        staged = self._path(INSTALLED_DIR) / ".staging"
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        _restore_capture(self.root, captured)
        _restore_snapshot(self.root)

    async def _install_with_pnpm(  # noqa: PLR0917
        self,
        selected_spec: str,
        parsed: Mapping[str, Any],
        result: ChangeResultDict,
        opts: Mapping[str, Any],
        control: Mapping[str, Any],
        request_id: str | None,
    ) -> None:
        registries = await self.registries()
        plan = registry_plan(cast(str | None, opts.get("registry")), registries)
        result["registries"] = list(plan)
        for name in (MANIFEST_FILE, "pnpm-lock.yaml"):
            current = self._path(name)
            if current.exists():
                self._path(f".{name}.snapshot").write_bytes(current.read_bytes())
        expected = opts.get("expected")
        for index, registry in enumerate(plan):
            control["signal"].throw_if_aborted()
            self.ctx.emit(
                INSTALL_STATE,
                {
                    "requestId": request_id,
                    "phase": "installing",
                    "attempt": {
                        "registry": registry,
                        "index": index + 1,
                        "total": len(plan),
                    },
                },
            )
            package_result = await operations.run_profile_pnpm(
                str(self.root),
                ["add", selected_spec],
                command=self._command,
                timeout_ms=self._inspect_timeout_ms,
                idle_timeout_ms=self._idle_timeout_ms,
                output_bytes=self._output_bytes,
                cancel_poll_ms=self._cancel_poll_ms,
                expected=expected if isinstance(expected, dict) else None,
                registry=registry,
                signal=control.get("signal"),
                on_output=lambda text: self.ctx.emit(
                    INSTALL_LOG, {"requestId": request_id, "text": text}
                ),
            )
            result["packageResult"] = cast(Any, package_result)
            if control["aborted"]:
                _restore_snapshot(self.root)
                raise InstallCancelledError()
            self.ctx.emit(
                INSTALL_LOG,
                {
                    "requestId": request_id,
                    "text": str(package_result.get("output", "")),
                    "exitCode": package_result.get("exitCode"),
                },
            )
            if package_result.get("exitCode") == 0:
                self._installed_from[selected_spec] = registry
                return
            failed_at = attribute_failure(
                str(package_result.get("kind", "")),
                str(package_result.get("output", "")),
                dict(parsed),
            )
            result["failedAt"] = cast(Any, failed_at)
            if failed_at != "registry" or index == len(plan) - 1:
                result["pendingBuilds"] = build_approval.record_pending_builds(
                    str(self.root), str(package_result.get("output", ""))
                )
                _restore_snapshot(self.root)
                raise ManagementFailure(
                    "operation-error", str(package_result.get("output", ""))
                )
        raise ManagementFailure("operation-error", "no registry answered")

    def _installed_from_spec(
        self,
        result: ChangeResultDict,
        opts: Mapping[str, Any],
        original: str,
        parsed: Mapping[str, Any],
        control: Mapping[str, Any],
    ) -> tuple[str, str | None]:
        expected = opts.get("expected")
        if isinstance(expected, dict) and isinstance(expected.get("name"), str):
            name = str(expected["name"])
            version = (
                str(expected.get("version"))
                if expected.get("version") is not None
                else None
            )
            deps = operations.direct_dependencies(self._manifest())
            if name not in deps:
                raise ManagementFailure("unexpected-package")
            return name, version
        before = set(cast(dict[str, str], control["beforeDependencies"]))
        current = operations.direct_dependencies(self._manifest())
        recorded = cast(dict[str, str], self._manifest()["dependencies"])
        added = set(current) - before
        if len(added) == 1:
            name = added.pop()
            return name, current.get(name)
        for candidate in recorded:
            if (
                install_spec.dependency_spec(
                    candidate, recorded[candidate], str(self.root)
                )
                == original
            ):
                return candidate, current.get(candidate)
        raise ManagementFailure("ambiguous-install")

    async def _install_local_source(
        self, parsed: Mapping[str, Any], result: ChangeResultDict, enabled: bool
    ) -> tuple[str, str | None]:
        source = Path(cast(str, parsed["path"])) if parsed["kind"] == "path" else None
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
        name, version = manifest.get("name"), manifest.get("version")
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
        return name, version if isinstance(version, str) else None

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

    @staticmethod
    def _package_manifest_from(
        source: Path, _parsed: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        try:
            data = json.loads((source / MANIFEST_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

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

    async def bundle_image(
        self,
        name: str,
        document: str,
        destination: str,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        """Read one image referenced by a bundle detail document."""
        rows = await self.list_bundles()
        bundle = next((item for item in rows if item["name"] == name), None)
        return bundle_detail.read_bundle_image(
            str(self.root),
            name,
            document,
            destination,
            installed=bundle,
            sources=list(sources or self._npm_sources),
            timeout_ms=self._timeout_ms,
            max_bytes=self._image_max_bytes,
        )

    async def catalog(self) -> list[dict[str, Any]]:
        """Official catalog rows for the current profile bundles."""
        return [
            catalog_module.bundle_catalog_entry(bundle)
            for bundle in await self.list_bundles()
        ]

    async def install_target(self, name: str) -> dict[str, str]:
        """Verified install target for one official bundle."""
        rows = await self.list_bundles()
        bundle = next((item for item in rows if item["name"] == name), None)
        if bundle is None:
            raise ManagementFailure("unknown-plugin")
        version = str(bundle.get("version", self._runtime_version))
        return dict(
            official_install_target.official_bundle_install_target(
                name, self._install_anchor, version
            )
        )

    async def read_bundle_detail(
        self, name: str, language: str, sources: Sequence[str] | None = None
    ) -> dict[str, Any]:
        self._guard()
        rows = await self.list_bundles()
        bundle = next((item for item in rows if item["name"] == name), None)
        if bundle is None:
            raise RemoteError(
                "plugin-manager/unknown-bundle", f"No such bundle: {name}", name=name
            )
        return bundle_detail.read_bundle_document(
            str(self.root),
            name,
            language,
            installed=bundle,
            sources=list(sources or self._npm_sources),
            timeout_ms=self._timeout_ms,
            max_bytes=self._document_max_bytes,
        )

    async def list_versions(
        self, name: str, options: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        self._guard()
        rows = await self.list_bundles()
        bundle = next((item for item in rows if item["name"] == name), None)
        source = str(bundle.get("source", "")) if bundle is not None else ""
        installed = str(bundle.get("version", "")) if bundle is not None else ""
        if (
            bundle is None
            or not bundle.get("installed")
            or not installed
            or pinned_git_origin(source) is not None
        ):
            return {
                "status": "refused",
                "problem": "not-updatable",
                "reason": f"{name} is not a registry-installed third-party bundle",
                "attempts": [],
            }
        parsed = parse_install_spec(source)
        if parsed["kind"] != "registry" or parsed["name"] != name:
            return {
                "status": "refused",
                "problem": "not-updatable",
                "reason": f"{name} has no registry source",
                "attempts": [],
            }
        registries = await self.registries()
        plan = registry_plan(None, registries)
        attempts: list[dict[str, Any]] = []
        answer: dict[str, Any] | None = None
        for index, registry in enumerate(plan):
            view = await operations.view_profile_package(
                name,
                versions.VERSION_LIST_FIELDS,
                str(self.root),
                registry,
                self._inspect_timeout_ms,
                self._command,
            )
            attempts.append({"registry": registry, **view})
            if view.get("exitCode") == 0 and view.get("answer") is not None:
                answer = view["answer"]
                break
            kind = install_failure.classify_install_failure(str(view.get("stderr", "")))
            if (
                attribute_failure(kind, str(view.get("stderr", "")), dict(parsed))
                != "registry"
            ):
                break
            del index
        if answer is None:
            return {
                "status": "refused",
                "problem": "network",
                "reason": "no registry answered",
                "attempts": attempts,
            }
        newer = versions.newer_versions(answer, installed)
        listed = newer[: self._version_list_limit]
        facts: list[dict[str, Any]] = []
        gate = asyncio.Semaphore(self._version_lookup_concurrency)

        async def detail_for(item: Mapping[str, Any]) -> dict[str, Any]:
            async with gate:
                return await operations.view_profile_package(
                    f"{name}@{item['version']}",
                    versions.VERSION_FIELDS,
                    str(self.root),
                    None,
                    self._inspect_timeout_ms,
                    self._command,
                )

        for item in listed:
            detail = await detail_for(item)
            if detail.get("exitCode") != 0:
                continue
            fact = versions.version_facts(
                detail.get("answer") or {}, name, item["version"]
            )
            issue = compatibility.evaluate_plugin_compatibility(
                fact,
                self.list_version_exemptions()["exemptions"],
                self._runtime_version,
            )
            if issue is not None and not issue["exempted"]:
                fact["incompatible"] = issue
            facts.append({**item, **fact})
        return {
            "status": "listed",
            "installed": installed,
            "registry": plan[0] if plan else None,
            "attempts": attempts,
            "versions": facts,
            "omitted": len(newer) - len(listed),
        }

    async def update_bundle(
        self, name: str, version: str, options: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        self._guard()
        opts = dict(options or {})
        self.ctx.emit(UPDATE_STATE, {"target": name, "phase": "checking"})
        listing = await self.list_versions(name)
        if listing.get("status") != "listed":
            return {"from": None, "to": version, "step": "check", "result": listing}
        installed = str(listing.get("installed", ""))
        if not version or not EXACT_VERSION.fullmatch(version):
            return {
                "from": installed,
                "to": version,
                "step": "check",
                "result": {
                    "changed": False,
                    "application": "failed",
                    "stage": "install",
                    "target": name,
                    "error": {"code": "invalid-spec"},
                },
            }
        result = await self.install_bundle(
            f"{name}@{version}",
            {
                "enabled": True,
                "expected": {"name": name, "version": version},
                **({"registry": opts["registry"]} if "registry" in opts else {}),
            },
        )
        return {"from": installed, "to": version, "step": "install", "result": result}

    async def wait_for_install(self, request_id: str) -> ChangeResultDict | None:
        self._guard()
        control = self._installs.get(request_id)
        return (
            None if control is None else cast(ChangeResultDict, control.get("result"))
        )

    async def cancel_install(self, request_id: str) -> dict[str, Any]:
        self._guard()
        control = self._installs.get(request_id)
        if control is None:
            return {"status": "not-running"}
        if control["phase"] == "applying":
            return {"status": "too-late"}
        self.ctx.emit(INSTALL_STATE, {"requestId": request_id, "phase": "cancelling"})
        control["aborted"] = True
        return {"status": "cancelled"}

    async def remove_bundle(self, name: str) -> ChangeResultDict:
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
            if (
                name in self._official
                or name in self._protected
                or name in self._management_bundles
            ):
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

        return await self._change("remove", name, step, reason="remove")


__all__ = [
    "CHANGED_EVENT",
    "COMPATIBILITY_FILE",
    "DEFAULT_IDLE_TIMEOUT_MS",
    "DEFAULT_INSPECT_TIMEOUT_MS",
    "DEFAULT_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_MS",
    "DEFAULT_VERSION_LIST_LIMIT",
    "DEFAULT_VERSION_LOOKUP_CONCURRENCY",
    "INSTALLED_DIR",
    "INSTALL_LOG",
    "INSTALL_STATE",
    "MANIFEST_FILE",
    "OPERATION_EVENT",
    "PATCHES_FILE",
    "UPDATE_STATE",
    "WORKSPACE_FILE",
    "PluginManager",
]
