"""Tests for verified upstream skills downloads."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from animas import Context

from mth_components.bridges.skills_registry.service import SkillsProviderRegistryService
from mth_components.upstream_skills import component
from mth_components.upstream_skills.service import (
    UpstreamSkillsService,
    apply,
    archive_url,
    digest_files,
    parse_entry,
    read_archive,
    validate_url,
)
from mth_components.upstream_skills.types import (
    UpstreamSkillRegistration,
    UpstreamSkillsError,
    UpstreamSkillSet,
    UpstreamSkillsHandle,
    UpstreamSkillsState,
)


def _tar(files: dict[str, bytes], *, unsafe: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo("repo-commit/" + name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        if unsafe is not None:
            info = tarfile.TarInfo("repo-commit/" + unsafe)
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
    return buffer.getvalue()


def _selection(
    files: dict[str, bytes], directory: str = "vendor/skills"
) -> UpstreamSkillSet:
    kept = {
        k: v
        for k, v in files.items()
        if k.startswith(directory + "/") or k == "LICENSE"
    }
    return UpstreamSkillSet(
        repository="vendor/repo",
        commit="a" * 40,
        directory=directory,
        license="LICENSE",
        bytes=sum(len(v) for v in kept.values()),
        sha256=digest_files(kept),
    )


def _entry(tmp_path: Path, name: str = "vendor-guide") -> Path:
    path = tmp_path / "SKILL.md"
    path.write_text(
        "---\nname: " + name + "\ndescription: Vendor guide\n---\n\nBody text.",
        encoding="utf-8",
    )
    return path


def test_url_and_archive_helpers() -> None:
    assert validate_url("https://codeload.github.com/") == "https://codeload.github.com"
    for bad in ("http://x", "https://u:p@x", "https://x?y=1", "https://x#y", 1):
        with pytest.raises(UpstreamSkillsError):
            validate_url(bad)
    selection = _selection({"LICENSE": b"l", "vendor/skills/a/SKILL.md": b"a"})
    assert archive_url("https://x", selection).endswith(
        "/vendor/repo/tar.gz/" + "a" * 40
    )
    assert (
        digest_files({"b": b"b", "a": b"a"})
        == hashlib.sha256(b"a\x001\x00a" + b"b\x001\x00b").hexdigest()
    )
    with pytest.raises(UpstreamSkillsError):
        UpstreamSkillSet(
            repository="bad",
            commit="a" * 40,
            directory="d",
            license="LICENSE",
            bytes=1,
            sha256="0" * 64,
        ).validate()
    assert selection.pin_name.startswith("skills-")


def test_parse_entry(tmp_path: Path) -> None:
    path = _entry(tmp_path)
    assert parse_entry(path) == ("vendor-guide", "Vendor guide", "Body text.")
    for content in (
        "no frontmatter",
        "---\nname: Bad_Name\ndescription: x\n---\nbody",
        "---\nname: ok\ndescription: ' '\n---\nbody",
    ):
        path.write_text(content, encoding="utf-8")
        with pytest.raises(UpstreamSkillsError):
            parse_entry(path)
    with pytest.raises(UpstreamSkillsError):
        parse_entry(tmp_path / "missing.md")


def test_read_archive() -> None:
    files = {"LICENSE": b"license", "vendor/skills/a/SKILL.md": b"skill"}
    selection = _selection(files)
    assert read_archive(_tar(files), selection) == files
    with pytest.raises(UpstreamSkillsError):
        read_archive(_tar(files, unsafe="../escape"), selection)
    with pytest.raises(UpstreamSkillsError):
        read_archive(
            _tar({"LICENSE": b"l", "other/a": b"a", "another/b": b"b"}), selection
        )
    with pytest.raises(UpstreamSkillsError):
        read_archive(
            _tar(files), UpstreamSkillSet(**{**selection.__dict__, "sha256": "0" * 64})
        )
    with pytest.raises(UpstreamSkillsError):
        read_archive(
            _tar(files), UpstreamSkillSet(**{**selection.__dict__, "bytes": 1})
        )
    with pytest.raises(UpstreamSkillsError):
        read_archive(_tar({"vendor/skills/a": b"a"}), selection)


def test_service_config() -> None:
    ctx = Context()
    for config in (
        {"skillsUrls": []},
        {"skillsUrls": "no"},
        {"skillsUrls": ["http://x"]},
        {"skillsTimeoutMs": 0},
        {"skillsTimeoutMs": True},
        {"skillsTimeoutMs": 2**31},
    ):
        with pytest.raises(UpstreamSkillsError):
            UpstreamSkillsService(ctx, config)
    service = apply(ctx)
    assert isinstance(service, UpstreamSkillsService)
    assert service.urls == ("https://codeload.github.com",)


def test_register_local_dir_and_duplicate(tmp_path: Path) -> None:

    ctx = Context()
    registry = SkillsProviderRegistryService(ctx)
    ctx.provide("skills_registry", registry)
    service = UpstreamSkillsService(ctx)
    entry = _entry(tmp_path)
    skills = tmp_path / "guides"
    skills.mkdir()
    selection = _selection({})
    options = UpstreamSkillRegistration("up", str(entry), selection, str(skills))
    handle = service.register(options)
    assert handle.state == "installed"
    assert handle.directory == str(skills.resolve())
    assert registry.get_skill("vendor-guide") is not None
    with pytest.raises(UpstreamSkillsError):
        service.register(options)
    bad = UpstreamSkillRegistration(
        "bad", str(entry), selection, str(tmp_path / "missing")
    )
    with pytest.raises(UpstreamSkillsError):
        service.register(bad)


@pytest.mark.asyncio
async def test_download_and_handle(tmp_path: Path) -> None:

    ctx = Context()
    registry = SkillsProviderRegistryService(ctx)
    ctx.provide("skills_registry", registry)
    env = patch.dict("os.environ", {"MTH_HOME": str(tmp_path / "home")})
    env.start()
    service = UpstreamSkillsService(
        ctx, {"skillsUrls": ["https://a.test", "https://b.test"]}
    )
    entry = _entry(tmp_path)
    files = {"LICENSE": b"license", "vendor/skills/a/SKILL.md": b"skill"}
    selection = _selection(files)
    options = UpstreamSkillRegistration("up", str(entry), selection)
    payload = _tar(files)
    calls: list[str] = []

    async def get(url: str) -> SimpleNamespace:
        calls.append(url)
        return SimpleNamespace(status_code=200, content=payload)

    client = AsyncMock()
    client.get.side_effect = get
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    handle = UpstreamSkillsHandle("up", str(tmp_path / "none"))
    with patch("httpx.AsyncClient", return_value=client):
        await service._download(ctx, options, handle)
    env.stop()
    assert handle.state == "installed"
    assert calls[0].startswith("https://a.test")
    assert Path(handle.directory).is_dir()
    assert registry.get_skill("vendor-guide") is not None

    observed: list[str] = []
    disposer = handle.watch(observed.append)
    handle.notify()
    assert observed == ["installed"]
    disposer()
    handle.notify()
    assert observed == ["installed"]
    if handle.dispose is not None:
        handle.dispose()


@pytest.mark.asyncio
async def test_download_failures(tmp_path: Path) -> None:
    ctx = Context()
    entry = _entry(tmp_path)
    files = {"LICENSE": b"l", "vendor/skills/a": b"a"}
    selection = _selection(files)
    service = UpstreamSkillsService(ctx, {"skillsUrls": ["https://a.test"]})
    client = AsyncMock()
    client.get.return_value = SimpleNamespace(status_code=500, content=b"")
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    with patch("httpx.AsyncClient", return_value=client):
        await service._download(
            ctx,
            UpstreamSkillRegistration("up", str(entry), selection),
            UpstreamSkillsHandle("up", str(tmp_path)),
        )
    bad = _tar({"LICENSE": b"l", "vendor/skills/a": b"a"}, unsafe="../bad")
    client.get.return_value = SimpleNamespace(status_code=200, content=bad)
    with patch("httpx.AsyncClient", return_value=client):
        await service._download(
            ctx,
            UpstreamSkillRegistration("up2", str(entry), selection),
            UpstreamSkillsHandle("up2", str(tmp_path)),
        )


def test_remaining_archive_branches() -> None:
    files = {"LICENSE": b"license", "vendor/skills/a/SKILL.md": b"skill"}
    selection = _selection(files)
    payload = _tar(files)
    with pytest.raises(UpstreamSkillsError):
        read_archive(_tar({**files, "vendor/skills/b/SKILL.md": b"dup"}), selection)
    duplicate = io.BytesIO()
    with tarfile.open(fileobj=duplicate, mode="w:gz") as archive:
        for _ in range(2):
            info = tarfile.TarInfo("repo-commit/LICENSE")
            info.size = 7
            archive.addfile(info, io.BytesIO(b"license"))
    with pytest.raises(UpstreamSkillsError):
        read_archive(duplicate.getvalue(), selection)
    with pytest.raises(UpstreamSkillsError):
        read_archive(
            _tar({"LICENSE": b"l", "vendor/skills/a": b"a"}),
            UpstreamSkillSet(**{**selection.__dict__, "bytes": 1}),
        )
    with pytest.raises(UpstreamSkillsError):
        read_archive(
            _tar(files),
            UpstreamSkillSet(
                **{**selection.__dict__, "license": "MISSING", "sha256": "0" * 64}
            ),
        )
    assert "LICENSE" in read_archive(payload, selection)


@pytest.mark.asyncio
async def test_component_and_handle_branches(tmp_path: Path) -> None:

    ctx = Context()
    registry = SkillsProviderRegistryService(ctx)
    ctx.provide("skills_registry", registry)
    await component.apply(ctx)
    assert ctx.upstream_skills is not None

    handle = UpstreamSkillsHandle("x", str(tmp_path), state="failed")
    observed: list[UpstreamSkillsState] = []
    d1 = handle.watch(observed.append)
    d2 = handle.watch(lambda state: (_ for _ in ()).throw(RuntimeError("observer")))
    handle.notify()
    assert observed == ["failed"]
    d1()
    d1()
    d2()
    handle.notify()
    assert observed == ["failed"]


@pytest.mark.asyncio
async def test_register_download_task_and_dispose(tmp_path: Path) -> None:

    env = patch.dict(os.environ, {"MTH_HOME": str(tmp_path / "home")})
    env.start()
    ctx = Context()
    entry = _entry(tmp_path)
    files = {"LICENSE": b"license", "vendor/skills/a/SKILL.md": b"skill"}
    selection = _selection(files)
    service = UpstreamSkillsService(ctx, {"skillsUrls": ["https://a.test"]})
    payload = _tar(files)
    client = AsyncMock()
    client.get.return_value = SimpleNamespace(status_code=200, content=payload)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    with patch("httpx.AsyncClient", return_value=client):
        handle = service.register(UpstreamSkillRegistration("x", str(entry), selection))
        if handle.retry is not None:
            handle.retry()
        task = service._tasks["vendor-guide"]
        await asyncio.gather(task, return_exceptions=True)
        env.stop()
        if handle.retry is not None:
            handle.retry()
        if handle.dispose is not None:
            handle.dispose()


def test_pin_validation_errors() -> None:
    for kwargs in (
        {"repository": "bad"},
        {"commit": "short"},
        {"directory": "dir/"},
        {"license": ""},
        {"sha256": "abc"},
    ):
        values = {
            "repository": "o/r",
            "commit": "a" * 40,
            "directory": "d",
            "license": "LICENSE",
            "bytes": 1,
            "sha256": "0" * 64,
        }
        values.update(kwargs)
        with pytest.raises(UpstreamSkillsError):
            UpstreamSkillSet(**values).validate()


def test_archive_edge_paths() -> None:
    files = {"LICENSE": b"license", "vendor/skills/a/SKILL.md": b"skill"}
    selection = _selection(files)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("repo-commit/")
        info.type = tarfile.DIRTYPE
        archive.addfile(info)
        info = tarfile.TarInfo("repo-commit/vendor/skills/dir")
        info.type = tarfile.DIRTYPE
        archive.addfile(info)
        info = tarfile.TarInfo("other/skills/x")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(UpstreamSkillsError):
        read_archive(buffer.getvalue(), selection)

    duplicate = io.BytesIO()
    with tarfile.open(fileobj=duplicate, mode="w:gz") as archive:
        for _ in range(2):
            info = tarfile.TarInfo("repo-commit/LICENSE")
            info.size = 7
            archive.addfile(info, io.BytesIO(b"license"))
    with pytest.raises(UpstreamSkillsError):
        read_archive(duplicate.getvalue(), selection)


@pytest.mark.asyncio
async def test_prune_retry_dispose_and_empty_urls(tmp_path: Path) -> None:

    ctx = Context()
    entry = _entry(tmp_path)
    files = {"LICENSE": b"license", "vendor/skills/a/SKILL.md": b"skill"}
    selection = _selection(files)
    service = UpstreamSkillsService(ctx)
    env = patch.dict(os.environ, {"MTH_HOME": str(tmp_path / "home")})
    env.start()
    options = UpstreamSkillRegistration("prune", str(entry), selection)
    target = service._directory(ctx, options)
    target.mkdir(parents=True)
    stale = target.parent / "skills-stale"
    stale.mkdir()
    service._prune(ctx, options, target)
    assert not stale.exists()
    local = UpstreamSkillRegistration("local", str(entry), selection, str(tmp_path))
    service._prune(ctx, local, target)
    with pytest.raises(UpstreamSkillsError):
        service._directory(
            ctx,
            UpstreamSkillRegistration(
                "bad", str(entry), selection, str(tmp_path / "nope")
            ),
        )
    env.stop()

    handle = service.register(options)
    if handle.retry is not None:
        handle.retry()
    if handle.dispose is not None:
        handle.dispose()

    empty = UpstreamSkillsService(ctx, {"skillsUrls": ["https://a.test"]})
    empty.urls = ()
    failed = UpstreamSkillsHandle("f", str(tmp_path))
    await empty._download(ctx, options, failed)


def test_archive_multi_root_and_extract_none(monkeypatch: Any) -> None:
    files = {"LICENSE": b"license", "vendor/skills/a/SKILL.md": b"skill"}
    selection = _selection(files)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in ("one/LICENSE", "two/skills/x"):
            info = tarfile.TarInfo(name)
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(UpstreamSkillsError):
        read_archive(buffer.getvalue(), selection)

    payload = _tar(files)
    original = tarfile.TarFile.extractfile

    def no_extract(self: Any, member: Any) -> None:
        return None

    monkeypatch.setattr(tarfile.TarFile, "extractfile", no_extract)
    with pytest.raises(UpstreamSkillsError):
        read_archive(payload, selection)
    monkeypatch.setattr(tarfile.TarFile, "extractfile", original)


@pytest.mark.asyncio
async def test_retry_with_local_dir_and_dispose_without_task(tmp_path: Path) -> None:
    ctx = Context()
    entry = _entry(tmp_path)
    skills = tmp_path / "local"
    skills.mkdir()
    selection = _selection({})
    service = UpstreamSkillsService(ctx)
    handle = service.register(
        UpstreamSkillRegistration("local-retry", str(entry), selection, str(skills))
    )
    assert handle.retry is not None
    handle.retry()
    service._tasks.pop("vendor-guide", None)
    if handle.dispose is not None:
        handle.dispose()
    other = UpstreamSkillsHandle("no-task", str(tmp_path))
    service._handles["no-task"] = other
    assert service._tasks.get("no-task") is None
