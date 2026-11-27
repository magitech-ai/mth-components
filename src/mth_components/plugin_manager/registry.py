"""Registry plans and failure attribution for package operations.

Ports ``packages/boot/plugin-manager/src/registry.ts``: the order registries
are asked, and which failures another registry could answer differently.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse, urlunparse

OFFICIAL_NPM_REGISTRY = "https://registry.npmjs.org/"
NPMMIRROR_REGISTRY = "https://registry.npmmirror.com/"
REGISTRY_URL = re.compile(r"^https?://\S+$")
NEXT_REGISTRY_KINDS = frozenset(
    {"network", "timeout", "not-found", "no-matching-version"}
)
ERROR_LINE = re.compile(
    "ERR_|ERROR|\\berror\\b|fatal:|Could not resolve|unable to access|ssh:|"
    "\\bE[A-Z]{4,}\\b"
)

Registry = str | None


def normalize_registry(url: str) -> str:
    """Normalize an http(s) registry URL to lowercase host with trailing slash."""
    try:
        parsed = urlparse(url)
    except ValueError as error:
        raise ValueError(f"a registry must be an http(s) URL: {url}") from error
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"a registry must be an http(s) URL: {url}")
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    userinfo_end = netloc.rfind("@") + 1
    host_part = netloc[userinfo_end:]
    default_port = ":443" if scheme == "https" else ":80"
    if host_part.endswith(default_port):
        host_part = host_part[: -len(default_port)]
    netloc = netloc[:userinfo_end] + host_part
    path = parsed.path or "/"
    if not path.endswith("/"):
        path = f"{path}/"
    return urlunparse(
        (scheme, netloc, path, parsed.params, parsed.query, parsed.fragment)
    )


def registry_plan(requested: Registry, configured: Any) -> list[Registry]:
    """The registries one operation asks, first to last; never empty."""
    resolved = configured.get("resolved")
    own = None if resolved is None else normalize_registry(resolved)
    fallbacks = [normalize_registry(item) for item in configured["fallbackRegistries"]]
    own_is_public = own is not None and (
        own == OFFICIAL_NPM_REGISTRY or own in fallbacks
    )

    def key_of(registry: Registry) -> str | None:
        return own if registry is None else normalize_registry(registry)

    known: list[Registry] = []
    keys: list[str | None] = []
    for registry in [configured["registry"], *configured["fallbackRegistries"]]:
        if registry is None and not own_is_public:
            continue
        key = key_of(registry)
        if key in keys:
            continue
        known.append(None if registry is None else normalize_registry(registry))
        keys.append(key)

    first = configured["registry"] if requested is None else requested
    first_key = key_of(first)
    normalized_first = None if first is None else normalize_registry(first)
    if (first is None or first_key == own) and not own_is_public:
        return [normalized_first]
    if first_key not in keys:
        return [normalized_first]
    return [
        normalized_first,
        *[item for index, item in enumerate(known) if keys[index] != first_key],
    ]


def attribute_failure(kind: str, log: str, spec: dict[str, Any]) -> str:
    """What a failed attempt could not reach or get an answer from."""
    if kind not in NEXT_REGISTRY_KINDS:
        return "other"
    host = spec.get("host")
    host = host.lower() if isinstance(host, str) else None
    if host is None:
        return "registry"
    named = any(
        ERROR_LINE.search(line) is not None and host in line.lower()
        for line in log.split("\n")
    )
    return "spec-host" if named else "registry"


__all__ = [
    "ERROR_LINE",
    "NEXT_REGISTRY_KINDS",
    "NPMMIRROR_REGISTRY",
    "OFFICIAL_NPM_REGISTRY",
    "REGISTRY_URL",
    "attribute_failure",
    "normalize_registry",
    "registry_plan",
]
