"""Core file-backed credential provider over a versioned YAML document.

The cross-process writer lock, hot-reload watcher and comment-preserving edits
of the pinned provider are deferred to a later phase; this core writes the
whole version-1 document atomically on each mutation.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any, cast

from magitech_harness.credentials import CredentialRef, ResolvedCredential

from mth_components.credentials_local.document import (
    _assert_key,
    _assert_ref,
    _parse_record,
    parse_document,
    render_document,
)

CREDENTIALS_FILENAME = ".credentials.yaml"
GROUP_OTHER_BITS = 0o077


def _parse_env_file(path: Path) -> dict[str, str]:
    """Read a dotenv file without ever writing to it."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name = name.strip()
        if name:
            values[name] = value.strip().strip('"').strip("'")
    return values


def resolve_spec(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve file location and watch settings from plugin config."""
    home = Path(
        config.get("dsh_home")
        or os.environ.get("MTH_HOME")
        or os.environ.get("MAGITECH_HOME")
        or Path.home() / ".magitech"
    )
    path = Path(config["path"]) if config.get("path") else home / CREDENTIALS_FILENAME
    return {
        "filename": path.resolve(strict=False),
        "home": home,
        "watch": bool(config.get("watch", True)),
        "debounce_ms": int(config.get("debounce_ms", 100)),
    }


def assert_owner_only(path: Path) -> None:
    """Refuse a document readable beyond its owner (POSIX only)."""
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return
    if sys.platform == "win32":
        return
    offending = mode & GROUP_OTHER_BITS
    if offending:
        raise PermissionError(
            f"credentials-local: {path} is readable beyond its owner "
            f'(mode {mode & 0o777:o}); run "chmod 600 {path}" before starting again'
        )


class LocalCredentialProvider:
    """Managed store plus environment layers, resolvable through the seam."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.refs: dict[str, str] = {}
        self.records: dict[str, dict[str, Any]] = {}
        self.ignored: dict[str, Any] = {}
        self.env_layers: list[dict[str, str]] = []
        self.load()

    @property
    def filename(self) -> Path:
        return cast(Path, self.spec["filename"])

    def load(self) -> None:
        """Replace the in-memory snapshot from the document and environment."""
        assert_owner_only(self.filename)
        try:
            text = self.filename.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        document = parse_document(text, str(self.filename))
        self.refs = dict(document["refs"])
        self.records = dict(document["records"])
        self.ignored = dict(document["ignored_keys"])
        layers: list[dict[str, str]] = [dict(os.environ)]
        layers.append(_parse_env_file(Path.cwd() / ".env"))
        layers.append(_parse_env_file(self.spec["home"] / ".env"))
        self.env_layers = layers

    def resolve(self, ref: CredentialRef) -> ResolvedCredential | None:
        """Resolve from managed store, then process, cwd and home environments."""
        name = str(ref)
        if name in self.refs:
            return ResolvedCredential(value=self.refs[name], source="stored")
        for layer in self.env_layers:
            value = layer.get(name)
            if value is not None and value != "":
                return ResolvedCredential(value=value, source="environment")
        return None

    def _write(self) -> None:
        self.filename.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        text = render_document(self.refs, self.records, self.ignored)
        tmp = self.filename.parent / f".{uuid.uuid4().hex}.tmp"

        def publish() -> None:
            try:
                descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            try:
                tmp.replace(self.filename)
                if sys.platform != "win32":
                    directory = os.open(self.filename.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise

        publish()

    def set_ref(self, ref: CredentialRef, value: str) -> None:
        """Store a reference and republish the document."""
        if not value:
            return
        self.refs[_assert_ref(str(ref))] = value
        self._write()

    def unset_ref(self, ref: CredentialRef) -> None:
        """Remove a stored reference and republish the document."""
        self.refs.pop(_assert_ref(str(ref)), None)
        self._write()

    def save_record(self, key: str, record: dict[str, Any] | None) -> None:
        """Write or remove a durable record and republish the document."""
        validated_key = _assert_key(key)
        if record is None:
            self.records.pop(validated_key, None)
        else:
            self.records[validated_key] = _parse_record(
                validated_key, dict(record), str(self.filename)
            )
        self._write()


__all__ = [
    "CREDENTIALS_FILENAME",
    "LocalCredentialProvider",
    "assert_owner_only",
    "resolve_spec",
]
