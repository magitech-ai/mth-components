"""Profile-owned configuration edits, serialized with HMR activity.

Ports the core of ``packages/boot/config-editor/src/index.ts``: read the
active profile rows, derive inherited/override config from composed layers,
validate and persist a whole-row replacement atomically, and apply it through
the loader. The cross-process file lock and YAML comment preservation of the
pinned provider are deferred to a later phase.
"""

from __future__ import annotations

import asyncio
import copy
import os
import sys
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from animas import Context, FiberState, Service
from animas.profile_yaml import dump_yaml, load_yaml

DEFAULT_PATCH_NAME = "profile.patch.yml"


def resolve_patch_path(config: Mapping[str, Any]) -> Path:
    """Resolve the profile patch document, never falling back to cwd."""
    if config.get("path"):
        return Path(config["path"]).resolve(strict=False)
    home = (
        config.get("dsh_home")
        or os.environ.get("MTH_HOME")
        or os.environ.get("MAGITECH_HOME")
        or Path.home() / ".magitech"
    )
    return (Path(home) / DEFAULT_PATCH_NAME).resolve(strict=False)


def _fsync_directory(path: Path) -> None:
    if sys.platform == "win32":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    """Replace the patch document atomically with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.parent / f".{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            dump_yaml(rows, handle)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
        _fsync_directory(path.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = load_yaml(path.read_text(encoding="utf-8"))
    if rows is None:
        return []
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError(
            f"config-editor: {path} must contain a YAML sequence of mappings"
        )
    return rows


def _flatten(rows: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, dict):
            continue
        result.append(row)
        nested = row.get("config")
        if isinstance(nested, list):
            result.extend(_flatten(nested))
    return result


class ConfigEditorService(Service):
    """Validate and persist whole-row configuration changes."""

    def __init__(self, ctx: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(ctx, "configEditor")
        self.config = dict(config or {})
        self._fallback_lock = asyncio.Lock()

    @property
    def document_path(self) -> Path:
        """Live path of the profile patch edited by this service."""
        return resolve_patch_path(self.config)

    def entries(self) -> list[Any]:
        """Addressable loader entries with unique ids."""
        loader = self.ctx.get("loader", None, strict=True)
        rows = list(loader.entries())
        seen: dict[str, int] = {}
        for entry in rows:
            options = getattr(entry, "options", None)
            identifier = options.get("id") if isinstance(options, dict) else None
            if isinstance(identifier, str):
                seen[identifier] = seen.get(identifier, 0) + 1
        addressable: list[Any] = []
        for entry in rows:
            options = getattr(entry, "options", None)
            identifier = options.get("id") if isinstance(options, dict) else None
            if isinstance(identifier, str) and seen[identifier] == 1:
                addressable.append(entry)
        return addressable

    def _layers(self) -> list[dict[str, Any]]:
        configured = self.config.get("layers", [])
        return _flatten(configured) if isinstance(configured, list) else []

    def _inherited(self, identifier: str) -> dict[str, Any]:
        composed: dict[str, Any] = {}
        for row in self._layers():
            if row.get("id") != identifier or "insert" in row:
                continue
            config = row.get("config")
            if isinstance(config, dict):
                composed.update(copy.deepcopy(config))
        return composed

    def configuration(self) -> list[dict[str, Any]]:
        """Detached inherited and override values for every active row."""
        overrides: dict[str, dict[str, Any]] = {}
        for row in _read_rows(self.document_path):
            if "insert" in row or not isinstance(row.get("id"), str):
                continue
            config = row.get("config")
            if isinstance(config, dict):
                overrides[row["id"]] = copy.deepcopy(config)
        result: list[dict[str, Any]] = []
        for entry in self.entries():
            identifier = entry.options["id"]
            override = overrides.get(identifier, {})
            result.append(
                {
                    "entry": entry,
                    "inherited": self._inherited(identifier),
                    "override": copy.deepcopy(override),
                }
            )
        return result

    async def edit(  # noqa: PLR0915
        self,
        entry: Any,
        change: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    ) -> None:
        """Validate, persist and apply one next configuration."""

        async def run() -> None:  # noqa: PLR0912
            before_entry = copy.deepcopy(dict(entry.options))
            if entry not in self.entries():
                raise ValueError(
                    "config-editor: configuration entry is no longer available"
                )
            fiber = getattr(entry, "fiber", None)
            if fiber is None or (
                getattr(fiber, "state", None) is not None
                and fiber.state is not FiberState.ACTIVE
            ):
                raise ValueError(
                    "config-editor: configuration plugin is no longer active"
                )
            identifier = entry.options["id"]
            current = copy.deepcopy(dict(entry.options.get("config") or {}))
            inherited = self._inherited(identifier)
            next_config = change(current, inherited)
            if not isinstance(next_config, dict):
                raise TypeError("config-editor: next configuration must be a mapping")
            overlays = self.config.get("overlays", [])
            if isinstance(overlays, list):
                effective = dict(inherited)
                effective.update(copy.deepcopy(next_config))
                for overlay in _flatten(overlays):
                    if overlay.get("id") == identifier and isinstance(
                        overlay.get("config"), dict
                    ):
                        effective.update(copy.deepcopy(overlay["config"]))
                if effective != next_config:
                    raise ValueError(
                        f'Configuration for "{identifier}" is overridden by a '
                        "home patch or command-line overlay"
                    )

            path = self.document_path
            rows = _read_rows(path)
            before = copy.deepcopy(rows)
            name = entry.options.get("name", identifier)
            matches = [
                position
                for position, row in enumerate(rows)
                if row.get("id") == identifier
                and "insert" not in row
                and ("name" not in row or row["name"] == name)
            ]
            if next_config == inherited:
                for position in reversed(matches):
                    rows[position].pop("config", None)
                    if set(rows[position]) <= {"id", "name"}:
                        rows.pop(position)
            elif matches:
                rows[matches[-1]]["config"] = copy.deepcopy(next_config)
            else:
                rows.append(
                    {
                        "id": identifier,
                        "name": name,
                        "config": copy.deepcopy(next_config),
                    }
                )
            loader = self.ctx.get("loader", None, strict=True)
            try:
                _write_atomic(path, rows)
                await loader.update(identifier, {"config": next_config})
            except BaseException:
                if "config" in before_entry:
                    entry.options["config"] = copy.deepcopy(before_entry["config"])
                else:
                    entry.options.pop("config", None)
                if before:
                    _write_atomic(path, before)
                else:
                    path.unlink(missing_ok=True)
                raise

        hmr = self.ctx.get("hmr", None, strict=False)
        if hmr is not None and hasattr(hmr, "run_exclusive"):
            await hmr.run_exclusive(run)
        elif hmr is not None:
            async with self._fallback_lock:
                await run()
        else:
            await run()


__all__ = ["DEFAULT_PATCH_NAME", "ConfigEditorService", "resolve_patch_path"]
