"""Domain vocabulary and stable failures for profile plugin management.

Ports the value types of ``packages/boot/plugin-manager/src/types.ts`` and
``error.ts``. Error codes are wire-stable: callers switch on them, never on
messages.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, TypedDict

PLUGIN_MANAGER_ERROR_CODES = (
    "PLUGIN_MANAGER_PROFILE_READ_FAILED",
    "PLUGIN_MANAGER_UNAVAILABLE",
)

MANAGEMENT_ERROR_CODES = (
    "management-required",
    "unaddressable",
    "unknown-plugin",
    "invalid-spec",
    "already-installed",
    "ambiguous-install",
    "not-bundle",
    "not-removable",
    "stop-profile",
    "bundle-in-use",
    "stale-approval",
    "incompatible-version",
    "unsupported-platform",
    "unexpected-package",
    "operation-error",
)

INSTALL_FAILURE_KINDS = (
    "pnpm-missing",
    "timeout",
    "not-found",
    "no-matching-version",
    "network",
    "disk-full",
    "permission",
    "build-blocked",
)

RESTART_REASONS = (
    "no-hmr",
    "package-moved",
    "host-module",
    "unmanaged-plugin",
    "module-comparison",
)

Application = Literal[
    "applied", "restart-required", "overridden", "failed", "cancelled"
]
Stage = Literal["install", "enable", "remove"]
ReadOnlyReason = Literal["management-required", "unaddressable"]


class PluginManagerError(Exception):
    """A typed plugin-management failure with a stable code."""

    name = "PluginManagerError"

    def __init__(
        self, message: str, code: str, cause: BaseException | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.__cause__ = cause


class InvalidInstallSpecError(Exception):
    """A spec pnpm or a registry would refuse, before any install starts."""

    name = "InvalidInstallSpecError"

    def __init__(self, spec: str, reason: str) -> None:
        super().__init__(f"plugin-manager: {reason}: {spec}")
        self.spec = spec
        self.reason = reason


class ManagementFailure(Exception):  # noqa: N818
    """An expected refusal reported through a ChangeResult rather than thrown."""

    def __init__(
        self,
        code: str,
        diagnostic: str | None = None,
        incompatible: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(diagnostic or code)
        self.code = code
        self.diagnostic = diagnostic
        self.incompatible = incompatible


class InstallCancelledError(Exception):
    """The caller stopped an installation; its files were restored."""


class ManagementErrorDict(TypedDict, total=False):
    code: str
    diagnostic: str
    incompatible: list[dict[str, Any]]


class ChangeResultDict(TypedDict, total=False):
    changed: bool
    application: Application
    restartReason: str
    stage: Stage
    target: str
    enabled: bool
    error: ManagementErrorDict
    warnings: list[str]
    packageResult: dict[str, Any]
    bundle: str
    version: str
    pendingBuilds: list[str]
    approvedBuilds: list[str]
    registries: list[str | None]
    failedAt: Literal["registry", "spec-host"]


class PackageResultDict(TypedDict, total=False):
    exitCode: int
    output: str
    truncated: bool
    logPath: str
    kind: str
    timedOut: bool
    incompatible: list[dict[str, Any]]
    unmet: Literal["unexpected-package", "not-bundle"]


class ParsedInstallSpec(TypedDict, total=False):
    kind: Literal["registry", "path", "tarball", "git"]
    spec: str
    name: str
    range: str
    path: str
    host: str


class PluginInfo(TypedDict, total=False):
    entryId: str
    moduleName: str
    enabled: bool
    patchId: str
    readOnlyReason: ReadOnlyReason


class BundleInfo(TypedDict, total=False):
    name: str
    official: bool
    availability: Literal["installation", "profile", "missing"]
    version: str
    description: str
    enabled: bool
    installed: bool
    source: str
    optional: bool
    removable: bool
    update: Literal["registry", "git"]
    readOnlyReason: ReadOnlyReason
    error: ManagementErrorDict
    rows: list[dict[str, Any]]
    overrides: list[str]


class PluginRegistries(TypedDict):
    registry: str | None
    fallbackRegistries: list[str]
    resolved: str | None


def management_error(error: BaseException) -> ManagementErrorDict:
    """Keep expected codes; report anything else as an operation error."""
    if isinstance(error, ManagementFailure):
        result: ManagementErrorDict = {"code": error.code}
        if error.diagnostic is not None:
            result["diagnostic"] = error.diagnostic
        if error.incompatible is not None:
            result["incompatible"] = error.incompatible
        return result
    return {
        "code": "operation-error",
        "diagnostic": str(error) if str(error) else type(error).__name__,
    }


def read_profile_state[T](read: Callable[[], T]) -> T:
    """Run one profile read, converting failures to a stable code."""
    try:
        return read()
    except PluginManagerError:
        raise
    except Exception as error:
        raise PluginManagerError(
            f"plugin-manager: failed to read the profile: {error}",
            "PLUGIN_MANAGER_PROFILE_READ_FAILED",
            error,
        ) from error


__all__ = [
    "INSTALL_FAILURE_KINDS",
    "MANAGEMENT_ERROR_CODES",
    "PLUGIN_MANAGER_ERROR_CODES",
    "RESTART_REASONS",
    "BundleInfo",
    "ChangeResultDict",
    "InstallCancelledError",
    "InvalidInstallSpecError",
    "ManagementErrorDict",
    "ManagementFailure",
    "PackageResultDict",
    "ParsedInstallSpec",
    "PluginInfo",
    "PluginManagerError",
    "PluginRegistries",
    "management_error",
    "read_profile_state",
]
