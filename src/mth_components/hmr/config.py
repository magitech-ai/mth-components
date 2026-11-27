"""dsh-style configuration for the animas HMR service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

DEFAULT_ROOT = ["."]
DEFAULT_IGNORED = ["**/node_modules", "**/.*", "cache", "data"]
DEFAULT_DEBOUNCE = 100


def resolve_hmr_config(config: dict[str, Any]) -> dict[str, Any]:
    """Map the dsh ``HmrConfig`` shape onto ``animas.HmrService`` config."""
    base = config.get("base") or "."
    if not isinstance(base, str):
        raise ValueError("hmr: 'base' must be a string")

    roots = config.get("root", DEFAULT_ROOT)
    if not isinstance(roots, (list, tuple)) or not roots:
        raise ValueError("hmr: 'root' must be a non-empty list of strings")
    if not all(isinstance(item, str) and item for item in roots):
        raise ValueError("hmr: 'root' entries must be non-empty strings")

    ignored = config.get("ignored", DEFAULT_IGNORED)
    if not isinstance(ignored, (list, tuple)) or not all(
        isinstance(item, str) for item in ignored
    ):
        raise ValueError("hmr: 'ignored' must be a list of strings")

    debounce = config.get("debounce", DEFAULT_DEBOUNCE)
    if isinstance(debounce, bool) or not isinstance(debounce, int):
        raise ValueError("hmr: 'debounce' must be an integer number of milliseconds")
    if debounce < 0:
        raise ValueError("hmr: 'debounce' must be >= 0")

    root_path = Path(base).resolve()
    mapped: dict[str, Any] = {
        "root": str(root_path),
        "roots": [str(root_path / item) for item in roots],
        "ignored": list(ignored),
        "debounce": debounce,
    }
    if "watch" in config:
        mapped["watch"] = config["watch"]
    if "full_reload" in config:
        mapped["full_reload"] = config["full_reload"]
    if "interval" in config:
        mapped["interval"] = config["interval"]
    return mapped


__all__ = ["DEFAULT_DEBOUNCE", "DEFAULT_IGNORED", "DEFAULT_ROOT", "resolve_hmr_config"]
