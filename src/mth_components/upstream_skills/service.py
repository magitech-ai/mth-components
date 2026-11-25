"""Upstream skills: verified, pinned downloads of vendor guide directories.

Ports ``packages/connector/upstream-skills/src``. Only DSH-authored entry skills
ship with the product; vendor guides are downloaded from a pinned GitHub
commit at registration, verified by size and SHA-256, and published under a
cache directory. Nothing downloads merely from mounting the service.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import shutil
import tarfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

import httpx
from animas import Context, Service

from mth_components.bridges.skills_registry.service import (
    SkillBundleInfo,
    SkillInvocation,
)
from mth_components.upstream_skills.types import (
    PIN_PREFIX,
    UpstreamSkillRegistration,
    UpstreamSkillsError,
    UpstreamSkillSet,
    UpstreamSkillsHandle,
)

DEFAULT_SKILLS_URLS = ("https://codeload.github.com",)
DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMER_DELAY_MS = 2_147_483_647
ENTRY_FRONT_MATTER = re.compile(r"^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)")
SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
BUNDLED_SKILL_RANK = 600


def validate_url(value: Any) -> str:
    """Require an HTTPS prefix without credentials, query or fragment."""
    if not isinstance(value, str):
        raise UpstreamSkillsError(
            "skillsUrls entries must be strings", "UPSTREAM_INVALID_CONFIG"
        )
    parsed = urlparse(value)
    if (
        not re.match(r"^https://[^\s?#@]+$", value)
        or parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise UpstreamSkillsError(
            f"skillsUrls: {value} is not an HTTPS URL without "
            "credentials, a query, or a fragment",
            "UPSTREAM_INVALID_CONFIG",
        )
    return value.rstrip("/")


def archive_url(prefix: str, selection: UpstreamSkillSet) -> str:
    """Build ``<prefix>/<owner>/<repo>/tar.gz/<commit>``."""
    return f"{prefix}/{selection.repository}/tar.gz/{selection.commit}"


def parse_entry(path: Path) -> tuple[str, str, str]:
    """Parse a packaged SKILL.md into (name, description, body)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise UpstreamSkillsError(
            f"cannot read entry skill {path}: {error}", "UPSTREAM_INVALID_ENTRY"
        ) from error
    match = ENTRY_FRONT_MATTER.match(raw)
    if match is None:
        raise UpstreamSkillsError(
            f"entry skill {path} has no valid frontmatter", "UPSTREAM_INVALID_ENTRY"
        )
    frontmatter = match.group(1)
    name = re.search(r"^name:\s*(.+?)\s*$", frontmatter, re.MULTILINE)
    description = re.search(r"^description:\s*(.+?)\s*$", frontmatter, re.MULTILINE)

    def unquote(value: str) -> str:
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            return value[1:-1].strip()
        return value

    resolved_name = unquote(name.group(1)) if name is not None else ""
    resolved_description = (
        unquote(description.group(1)) if description is not None else ""
    )
    if not SKILL_NAME.match(resolved_name):
        raise UpstreamSkillsError(
            f"entry skill {path} has an invalid name", "UPSTREAM_INVALID_ENTRY"
        )
    if not resolved_description:
        raise UpstreamSkillsError(
            f"entry skill {path} has an empty description", "UPSTREAM_INVALID_ENTRY"
        )
    return resolved_name, resolved_description, raw[match.end() :].strip()


def digest_files(files: Mapping[str, bytes]) -> str:
    """SHA-256 over sorted path/length/content triples (not archive bytes)."""
    digest = hashlib.sha256()
    for path in sorted(files):
        content = files[path]
        digest.update(f"{path}\0{len(content)}\0".encode())
        digest.update(content)
    return digest.hexdigest()


def _safe_segments(path: str) -> bool:
    segments = path.rstrip("/").split("/")
    return all(
        segment and segment not in (".", "..") and "\\" not in segment
        for segment in segments
    )


def read_archive(payload: bytes, selection: UpstreamSkillSet) -> dict[str, bytes]:
    """Read and validate the kept files of a gzip tarball."""
    kept: dict[str, bytes] = {}
    total = 0
    root: str | None = None
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive:
            raw_path = member.name
            if not _safe_segments(raw_path):
                raise UpstreamSkillsError(
                    f"unsafe archive path {raw_path!r}", "UPSTREAM_INVALID_ARCHIVE"
                )
            segments = raw_path.rstrip("/").split("/")
            top = segments[0]
            if root is None:
                root = top
            elif top != root:
                raise UpstreamSkillsError(
                    "archive contains multiple root directories",
                    "UPSTREAM_INVALID_ARCHIVE",
                )
            path = "/".join(segments[1:])
            if not path:
                continue
            keep = path == selection.license or path.startswith(
                f"{selection.directory}/"
            )
            if not keep or any(path == exclude for exclude in selection.exclude):
                continue
            if not member.isfile():
                raise UpstreamSkillsError(
                    f"kept archive path {path} is not a regular file",
                    "UPSTREAM_INVALID_ARCHIVE",
                )
            if path in kept:
                raise UpstreamSkillsError(
                    f"duplicate archive path {path}", "UPSTREAM_INVALID_ARCHIVE"
                )
            total += member.size
            if total > selection.bytes:
                raise UpstreamSkillsError(
                    "archive exceeds the pinned byte budget", "UPSTREAM_INVALID_ARCHIVE"
                )
            extracted = archive.extractfile(member)
            if extracted is None:
                raise UpstreamSkillsError(
                    f"cannot read archive path {path}", "UPSTREAM_INVALID_ARCHIVE"
                )
            kept[path] = extracted.read()
    if selection.license not in kept:
        raise UpstreamSkillsError(
            "archive is missing the pinned license", "UPSTREAM_INVALID_ARCHIVE"
        )
    if digest_files(kept) != selection.sha256:
        raise UpstreamSkillsError(
            "archive digest does not match the pin", "UPSTREAM_DIGEST_MISMATCH"
        )
    return kept


class UpstreamSkillsService(Service):
    """``ctx.upstream_skills``: verified upstream skill-set registration."""

    inject: ClassVar[list[str]] = ["skills_registry"]

    def __init__(self, ctx: Context, config: Mapping[str, Any] | None = None) -> None:
        super().__init__(ctx, "upstream_skills")
        cfg = dict(config or {})
        urls = cfg.get("skillsUrls", DEFAULT_SKILLS_URLS)
        if not isinstance(urls, (list, tuple)) or not urls:
            raise UpstreamSkillsError(
                "skillsUrls must be a non-empty array", "UPSTREAM_INVALID_CONFIG"
            )
        self.urls = tuple(validate_url(value) for value in urls)
        timeout = cfg.get("skillsTimeoutMs", DEFAULT_TIMEOUT_MS)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or timeout < 1
            or timeout > MAX_TIMER_DELAY_MS
        ):
            raise UpstreamSkillsError(
                "skillsTimeoutMs must be a positive safe integer",
                "UPSTREAM_INVALID_CONFIG",
            )
        self.timeout_ms = timeout
        self._handles: dict[str, UpstreamSkillsHandle] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    def _cache_root(self, caller: Context) -> Path:
        home = os.environ.get("MTH_HOME") or os.environ.get("DSH_HOME")
        base = Path(home) if home else Path.home() / ".magitech"
        return base / "cache"

    def _directory(self, caller: Context, options: UpstreamSkillRegistration) -> Path:
        if options.skills_dir is not None:
            directory = Path(options.skills_dir).resolve()
            if not directory.is_dir():
                raise UpstreamSkillsError(
                    f"skillsDir {options.skills_dir} is not a directory",
                    "UPSTREAM_INVALID_SKILLS_DIR",
                )
            return directory
        root = self._cache_root(caller) / options.name
        return root / options.set.pin_name

    def _publish(
        self,
        caller: Context,
        options: UpstreamSkillRegistration,
        files: Mapping[str, bytes],
    ) -> Path:
        target = self._directory(caller, options)
        staging = target.parent / f".{target.name}-{uuid.uuid4().hex}"
        try:
            for path, content in files.items():
                destination = staging / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            staging.replace(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        self._prune(caller, options, target)
        return target

    def _prune(
        self, caller: Context, options: UpstreamSkillRegistration, target: Path
    ) -> None:
        if options.skills_dir is not None:
            return
        pins = target.parent
        for sibling in pins.iterdir():
            if (
                sibling.name.startswith(PIN_PREFIX)
                and sibling != target
                and sibling.is_dir()
            ):
                shutil.rmtree(sibling, ignore_errors=True)

    async def _download(
        self,
        caller: Context,
        options: UpstreamSkillRegistration,
        handle: UpstreamSkillsHandle,
    ) -> None:
        handle.state = "downloading"
        handle.notify()
        last_error: Exception | None = None
        for prefix in self.urls:
            url = archive_url(prefix, options.set)
            try:
                async with httpx.AsyncClient(timeout=self.timeout_ms / 1000) as client:
                    response = await client.get(url)
                if response.status_code < 200 or response.status_code >= 300:
                    raise UpstreamSkillsError(
                        f"HTTP {response.status_code}", "UPSTREAM_HTTP_ERROR"
                    )
                files = read_archive(response.content, options.set)
                directory = self._publish(caller, options, files)
                handle.directory = str(directory)
                handle.state = "installed"
                self._register_skill(
                    caller, options, handle, parse_entry(Path(options.entry))
                )
                handle.notify()
                return
            except Exception as error:
                last_error = error
        handle.state = "failed"
        handle.notify()
        if last_error is not None:
            self.ctx.logger.warning(
                "downloading the upstream skills from %s failed: %s",
                options.set.repository,
                last_error,
            )

    def _register_skill(
        self,
        caller: Context,
        options: UpstreamSkillRegistration,
        handle: UpstreamSkillsHandle,
        entry: tuple[str, str, str],
    ) -> None:
        registry = caller.get("skills_registry", None, strict=False)
        if registry is None:
            return
        name, description, body = entry
        registry.register_skill(
            SkillBundleInfo(
                name=name,
                description=description,
                path=Path(handle.directory),
                skill_file=Path(options.entry),
                body=body,
                invocation=SkillInvocation(True, True),
                rank=BUNDLED_SKILL_RANK,
            )
        )

    def register(self, options: UpstreamSkillRegistration) -> UpstreamSkillsHandle:
        """Register one pinned skill set in the calling plugin's lifecycle."""
        caller = self.ctx
        options.set.validate()
        name, description, body = parse_entry(Path(options.entry))
        if name in self._handles:
            raise UpstreamSkillsError(
                f'upstream skill provider "{name}" already exists', "UPSTREAM_DUPLICATE"
            )
        directory = self._directory(caller, options)
        installed = directory.is_dir()
        handle = UpstreamSkillsHandle(
            name=name,
            directory=str(directory),
            state="installed" if installed else "failed",
        )

        def retry() -> None:
            if options.skills_dir is not None or handle.state == "downloading":
                return
            task = asyncio.create_task(self._download(caller, options, handle))
            self._tasks[name] = task

        def dispose() -> None:
            task = self._tasks.pop(name, None)
            if task is not None:
                task.cancel()
            self._handles.pop(name, None)

        handle.retry = retry
        handle.dispose = dispose
        self._handles[name] = handle
        if installed:
            self._register_skill(caller, options, handle, (name, description, body))
        else:
            retry()
        return handle


def apply(
    ctx: Context, config: Mapping[str, Any] | None = None
) -> UpstreamSkillsService:
    """Mount ``ctx.upstream_skills``."""
    return UpstreamSkillsService(ctx, config)


apply.inject = ["skills_registry"]  # type: ignore[attr-defined]

__all__ = [
    "BUNDLED_SKILL_RANK",
    "DEFAULT_SKILLS_URLS",
    "DEFAULT_TIMEOUT_MS",
    "UpstreamSkillsService",
    "apply",
    "archive_url",
    "digest_files",
    "parse_entry",
    "read_archive",
    "validate_url",
]
