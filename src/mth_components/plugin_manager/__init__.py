"""Core profile plugin and bundle management."""

from __future__ import annotations

from mth_components.plugin_manager.install_spec import (
    InvalidInstallSpecError,
    dependency_spec,
    parse_install_spec,
    pinned_git_origin,
)
from mth_components.plugin_manager.registry import (
    normalize_registry,
    registry_plan,
)
from mth_components.plugin_manager.service import PluginManager
from mth_components.plugin_manager.types import (
    PLUGIN_MANAGER_ERROR_CODES,
    ManagementFailure,
    PluginManagerError,
)

__all__ = [
    "PLUGIN_MANAGER_ERROR_CODES",
    "InvalidInstallSpecError",
    "ManagementFailure",
    "PluginManager",
    "PluginManagerError",
    "dependency_spec",
    "normalize_registry",
    "parse_install_spec",
    "pinned_git_origin",
    "registry_plan",
]
