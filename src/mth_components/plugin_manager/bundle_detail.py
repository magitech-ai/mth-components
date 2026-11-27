"""Bundle detail documents and images.

Ports ``packages/boot/plugin-manager/src/bundle-detail.ts`` and the
``@deepseek-ai/dsh-plugin-detail`` readers it uses: a bundle's long description
comes from the locale ``meta.detail`` file or a README, installed bundles are
read from their directory, and offered bundles from npm file CDNs. Nothing is
cached.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

import httpx
import yaml  # type: ignore[import-untyped]

from mth_components.plugin_manager.types import RemoteError

LANGUAGE_PATTERN = re.compile(r"^[a-z]{2,8}(?:-[a-z0-9]{1,8})*$")
MARKDOWN_PATTERN = re.compile(r"\.(?:md|markdown)$", re.IGNORECASE)
FRONT_MATTER = re.compile(r"^---\r?\n[\s\S]*?\r?\n---[ \t]*(?:\r?\n|$)")
README_NAMES = ("README.md", "readme.md", "Readme.md", "README.markdown", "README")
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
}
DEFAULT_DOCUMENT_BYTES = 1024 * 1024
DEFAULT_IMAGE_BYTES = 5 * 1024 * 1024


def language_chain(language: str) -> list[str]:
    """Fallback chain from a requested locale to every prefix and English."""
    lowered = language.lower()
    if not LANGUAGE_PATTERN.fullmatch(lowered):
        raise RemoteError(
            "plugin-manager/invalid-language",
            "Invalid display language",
            language=language,
        )
    parts = lowered.split("-")
    chain = ["-".join(parts[:length]) for length in range(len(parts), 0, -1)]
    if "en" not in chain:
        chain.append("en")
    return chain


def detail_path(value: Any, label: str) -> str | None:
    """Validate a locale ``meta.detail`` path, or return ``None`` when absent."""
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\\" in value
        or re.match(r"^[A-Za-z][A-Za-z\d+.-]*:", value)
        or any(part in ("", ".", "..") for part in value.split("/"))
        or not MARKDOWN_PATTERN.search(value)
    ):
        raise RemoteError(
            "plugin-manager/detail-unavailable",
            f"{label} must name a Markdown file inside the package",
            reason="invalid",
        )
    return value


def markdown_of(data: bytes) -> str:
    """Decode Markdown without a BOM or a leading YAML front-matter block."""
    text = data.decode("utf-8", errors="replace").removeprefix("\ufeff")
    return FRONT_MATTER.sub("", text, count=1)


def _read(bundle_dir: Path, relative: str, max_bytes: int) -> bytes | None:
    candidate = bundle_dir / relative
    try:
        if not candidate.is_file() or candidate.stat().st_size > max_bytes:
            return None
        return candidate.read_bytes()[:max_bytes]
    except OSError:
        return None


def _locale_detail(bundle_dir: Path, language: str, max_bytes: int) -> str | None:
    for locale in language_chain(language):
        names = {locale}
        match = re.fullmatch(r"([a-z]{2,3})-([a-z]{2})", locale)
        if match is not None:
            names.add(f"{match.group(1)}-{match.group(2).upper()}")
        for name in sorted(names):
            data = _read(bundle_dir, f"locale/{name}.json", max_bytes)
            if data is None:
                continue
            try:
                parsed = json.loads(data.decode("utf-8", errors="replace"))
            except ValueError as error:
                raise RemoteError(
                    "plugin-manager/detail-unavailable",
                    f"locale/{name}.json is not valid JSON: {error}",
                    reason="invalid",
                ) from error
            meta = parsed.get("meta") if isinstance(parsed, dict) else None
            declared = meta.get("detail") if isinstance(meta, dict) else None
            detail = detail_path(declared, f"locale/{name}.json: meta.detail")
            if detail is not None:
                return detail
            break
    return None


def _readme_name(bundle_dir: Path) -> str | None:
    try:
        names = {item.name for item in bundle_dir.iterdir() if item.is_file()}
    except OSError:
        return None
    ranked = [
        name
        for name in names
        if re.fullmatch(r"readme(?:\.md|\.markdown)?", name, re.IGNORECASE)
    ]
    ranked.sort(
        key=lambda name: (
            README_NAMES.index(name) if name in README_NAMES else len(README_NAMES),
            name,
        )
    )
    return ranked[0] if ranked else None


def _local_detail_file(
    bundle_dir: Path,
    meta: Mapping[str, Any],
    language: str,
    max_bytes: int = DEFAULT_DOCUMENT_BYTES,
) -> str | None:
    declared = _locale_detail(bundle_dir, language, max_bytes)
    if declared is None:
        declared = detail_path(meta.get("detail"), "package.json: meta.detail")
    if declared is not None:
        return declared if _read(bundle_dir, declared, max_bytes) is not None else None
    return _readme_name(bundle_dir)


def _meta(bundle_dir: Path) -> dict[str, Any]:
    path = bundle_dir / "package.json"
    try:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    if not isinstance(manifest, dict):
        return {}
    meta = manifest.get("meta")
    return meta if isinstance(meta, dict) else {}


def read_bundle_document(
    root: str,
    name: str,
    language: str,
    *,
    installed: Mapping[str, Any] | None = None,
    sources: list[str] | None = None,
    timeout_ms: int = 15_000,
    max_bytes: int = DEFAULT_DOCUMENT_BYTES,
) -> dict[str, Any]:
    """Return the document origin and Markdown, or ``status: none``."""
    language_chain(language)
    bundle_dir = Path(root, "installed", name)
    meta = _meta(bundle_dir)
    detail_path_value = _local_detail_file(bundle_dir, meta, language, max_bytes)
    if detail_path_value is not None:
        data = _read(bundle_dir, detail_path_value, max_bytes)
        if data is not None:
            return {
                "status": "found",
                "origin": "local",
                "path": detail_path_value,
                "markdown": markdown_of(data),
            }
    target = (installed or {}).get("installTarget") or {}
    version = target.get("version") if isinstance(target, dict) else None
    candidates = [detail_path_value] if detail_path_value else []
    candidates.extend(name for name in README_NAMES if name not in candidates)
    for source in sources or []:
        base = source.rstrip("/")
        for relative in candidates:
            urls = (
                [
                    f"{base}/npm/{name}@{version}/{relative}",
                    f"{base}/{name}/{version}/files/{relative}",
                ]
                if isinstance(version, str)
                else [f"{base}/{name}/{relative}"]
            )
            for url in urls:
                try:
                    response = httpx.get(
                        url, timeout=timeout_ms / 1000, follow_redirects=False
                    )
                except httpx.HTTPError:
                    continue
                if response.status_code >= 300:
                    continue
                return {
                    "status": "found",
                    "origin": source,
                    "path": relative,
                    "markdown": markdown_of(response.content[:max_bytes]),
                }
    return {"status": "none"}


def detail_image(document: str, authored: str) -> tuple[str, str] | None:
    """Resolve an authored image path to a root-relative path and media type."""
    stripped = authored.split("#", 1)[0].split("?", 1)[0]
    if re.search(r"%(?![0-9A-Fa-f]{2})", stripped) is not None:
        return None
    decoded = unquote(stripped)
    if "\\" in decoded or "\0" in decoded:
        return None
    path = PurePosixPath(
        decoded.lstrip("/")
        if decoded.startswith("/")
        else str(PurePosixPath(document).parent / decoded)
    )
    if ".." in path.parts or path.parts in ((), (".",)):
        return None
    relative = str(path)
    media_type = IMAGE_TYPES.get(path.suffix.lower())
    return None if media_type is None else (relative, media_type)


def read_bundle_image(
    root: str,
    name: str,
    document: str,
    destination: str,
    *,
    installed: Mapping[str, Any] | None = None,
    sources: list[str] | None = None,
    timeout_ms: int = 15_000,
    max_bytes: int = DEFAULT_IMAGE_BYTES,
) -> dict[str, Any]:
    """Return one image referenced by a detail document."""
    resolved = detail_image(document, destination)
    if resolved is None:
        raise RemoteError(
            "plugin-manager/detail-unavailable",
            "Invalid image path",
            reason="invalid-path",
        )
    relative, media_type = resolved
    path = Path(root, "installed", name, relative)
    if path.is_file():
        return {"bytes": path.read_bytes()[:max_bytes], "mediaType": media_type}
    target = (installed or {}).get("installTarget") or {}
    version = target.get("version") if isinstance(target, dict) else None
    for source in sources or []:
        base = source.rstrip("/")
        urls = (
            [
                f"{base}/npm/{name}@{version}/{relative}",
                f"{base}/{name}/{version}/files/{relative}",
            ]
            if isinstance(version, str)
            else [f"{base}/{name}/{relative}"]
        )
        for url in urls:
            try:
                response = httpx.get(
                    url, timeout=timeout_ms / 1000, follow_redirects=False
                )
            except httpx.HTTPError:
                continue
            if response.status_code < 300:
                return {
                    "bytes": response.content[:max_bytes],
                    "mediaType": media_type,
                }
    raise RemoteError(
        "plugin-manager/detail-unavailable", "Image is unavailable", reason="not-found"
    )


__all__ = [
    "DEFAULT_DOCUMENT_BYTES",
    "DEFAULT_IMAGE_BYTES",
    "README_NAMES",
    "detail_image",
    "detail_path",
    "language_chain",
    "markdown_of",
    "read_bundle_document",
    "read_bundle_image",
]
