"""Atomic whole-file replacement for the JSON storage backend."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path


def _publish(path: Path, data: str) -> None:
    tmp = path.parent / f".{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
        fsync_directory(path.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def mkdir_private(path: Path) -> None:
    """Create *path* and every missing parent with mode 0700 (dsh recursive mode)."""
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts:
        current = current / part
        current.mkdir(mode=0o700, exist_ok=True)


def fsync_directory(path: Path) -> None:
    """Persist directory-entry changes on POSIX; no-op on Windows."""
    if sys.platform == "win32":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


async def write_atomic(path: Path, data: str) -> None:
    """Durably replace *path* with *data* (fsync file, rename, fsync dir)."""
    await asyncio.to_thread(_publish, path, data)
