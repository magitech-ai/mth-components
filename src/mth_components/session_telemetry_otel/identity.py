"""Harness-home-scoped anonymous user id.

Ports ``packages/identity/anonymous-user-id/src/index.ts``: a random UUID
persisted as a bare line in ``.anonymous-user-id`` below the harness home.
The id is never derived from a hostname, address, or git remote; deleting the
file mints a fresh identity. Reads and writes are synchronous and memoized per
resolved file path, and persistence is best-effort so a read-only home never
blocks telemetry.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from pathlib import Path

from magitech_harness.shell_env import resolve_magitech_home

ANONYMOUS_USER_ID_FILE_NAME = ".anonymous-user-id"
_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_memo: dict[str, str] = {}


def _read_persisted_id(path: Path) -> str | None:
    try:
        candidate = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return candidate if _UUID_PATTERN.fullmatch(candidate) else None


def get_or_create_anonymous_user_id(
    home: str | None = None,
    random_uuid: Callable[[], str] | None = None,
) -> str:
    """Return the harness home's id, creating and persisting one on first use."""
    resolved = home if home is not None else resolve_magitech_home()
    path = Path(resolved) / ANONYMOUS_USER_ID_FILE_NAME
    key = str(path)
    cached = _memo.get(key)
    if cached is not None:
        return cached

    identifier = _read_persisted_id(path)
    if identifier is None:
        created = random_uuid() if random_uuid is not None else str(uuid.uuid4())
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as handle:
                handle.write(f"{created}\n")
            identifier = created
        except OSError:
            identifier = _read_persisted_id(path)
            if identifier is None:
                try:
                    path.write_text(f"{created}\n", encoding="utf-8")
                except OSError:
                    pass
                identifier = created
    _memo[key] = identifier
    return identifier


__all__ = ["ANONYMOUS_USER_ID_FILE_NAME", "get_or_create_anonymous_user_id"]
