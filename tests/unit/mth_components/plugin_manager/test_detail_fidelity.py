"""Fidelity probes for locale documents, READMEs, and image resolution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from animas import Context

from mth_components.plugin_manager import bundle_detail, official_install_target
from mth_components.plugin_manager.service import PluginManager
from mth_components.plugin_manager.types import RemoteError


def _bundle(tmp_path: Path) -> Path:
    directory = tmp_path / "installed" / "demo"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text("{}", encoding="utf-8")
    return directory


def test_locale_detail_and_markdown(tmp_path: Path) -> None:
    directory = _bundle(tmp_path)
    locale = directory / "locale"
    locale.mkdir()
    (locale / "zh-CN.json").write_text(
        json.dumps({"meta": {"detail": "docs/guide.md"}}), encoding="utf-8"
    )
    (directory / "docs").mkdir()
    (directory / "docs" / "guide.md").write_bytes(
        b"\xef\xbb\xbf---\ntitle: Guide\n---\n# Body\n"
    )
    found = bundle_detail.read_bundle_document(str(tmp_path), "demo", "zh-CN")
    assert found["status"] == "found"
    assert found["path"] == "docs/guide.md"
    assert found["markdown"] == "# Body\n"
    with pytest.raises(RemoteError, match="inside the package"):
        bundle_detail.detail_path("../secret.md", "meta.detail")


def test_locale_malformed_json_and_missing_target(tmp_path: Path) -> None:
    directory = _bundle(tmp_path)
    (directory / "locale").mkdir()
    (directory / "locale" / "en.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(RemoteError, match="not valid JSON"):
        bundle_detail.read_bundle_document(str(tmp_path), "demo", "en")

    (directory / "locale" / "en.json").write_text(
        json.dumps({"meta": {}}), encoding="utf-8"
    )
    assert bundle_detail._local_detail_file(directory, {}, "en") is None
    (directory / "locale" / "en.json").write_text(
        json.dumps({"meta": {"detail": "missing.md"}}), encoding="utf-8"
    )
    assert bundle_detail._local_detail_file(directory, {}, "en") is None


def test_detail_read_failure_falls_back_to_cdn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _bundle(tmp_path)
    (directory / "docs").mkdir()
    (directory / "docs" / "guide.md").write_text("# Guide", encoding="utf-8")
    (directory / "package.json").write_text(
        json.dumps({"meta": {"detail": "docs/guide.md"}}), encoding="utf-8"
    )
    original = bundle_detail._read
    calls = 0

    def flaky(bundle_dir: Path, relative: str, max_bytes: int) -> bytes | None:
        nonlocal calls
        calls += 1
        return None if calls >= 3 else original(bundle_dir, relative, max_bytes)

    monkeypatch.setattr(bundle_detail, "_read", flaky)
    answer = bundle_detail.read_bundle_document(str(tmp_path), "demo", "en")
    assert answer == {"status": "none"}


def test_readme_variants_and_size_limit(tmp_path: Path) -> None:
    directory = _bundle(tmp_path)
    (directory / "readme.md").write_text("Readme", encoding="utf-8")
    found = bundle_detail.read_bundle_document(str(tmp_path), "demo", "en")
    assert found["status"] == "found" and found["markdown"] == "Readme"
    assert bundle_detail._read(directory, "readme.md", 1) is None


def test_read_os_error_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _bundle(tmp_path)

    def fail(self: Path) -> Any:
        raise OSError("unreadable")

    monkeypatch.setattr(Path, "stat", fail)
    assert bundle_detail._read(directory, "package.json", 1024) is None


def test_detail_image_types_and_encoding() -> None:
    assert bundle_detail.detail_image("docs/guide.md", "a.svg?x=1#f") == (
        "docs/a.svg",
        "image/svg+xml",
    )
    assert bundle_detail.detail_image("docs/guide.md", "/images/a.avif") == (
        "images/a.avif",
        "image/avif",
    )
    assert bundle_detail.detail_image("docs/guide.md", "a%20b.png") == (
        "docs/a b.png",
        "image/png",
    )
    assert bundle_detail.detail_image("docs/guide.md", "bad%.png") is None
    assert bundle_detail.detail_image("docs/guide.md", "a\\b.png") is None
    assert bundle_detail.detail_image("docs/guide.md", "../a.png") is None
    assert bundle_detail.detail_image("docs/guide.md", "a.txt") is None


def test_cdn_version_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []

    class Response:
        status_code = 404
        content = b""

        def __init__(self, url: str, **_k: Any) -> None:
            urls.append(url)

    monkeypatch.setattr(bundle_detail.httpx, "get", Response)
    answer = bundle_detail.read_bundle_document(
        str(tmp_path),
        "demo",
        "en",
        installed={"installTarget": {"version": "1.2.3"}},
        sources=["https://cdn/"],
    )
    assert answer == {"status": "none"}
    assert "https://cdn/npm/demo@1.2.3/README.md" in urls
    assert "https://cdn/demo/1.2.3/files/README.md" in urls


@pytest.mark.asyncio
async def test_service_uses_versioned_cdn_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        official_install_target,
        "official_bundle_install_target",
        lambda *_a, **_k: {"spec": "demo@1.2.3", "version": "1.2.3"},
    )
    urls: list[str] = []

    class Response:
        status_code = 404
        content = b""

        def __init__(self, url: str, **_k: Any) -> None:
            urls.append(url)

    monkeypatch.setattr(bundle_detail.httpx, "get", Response)
    manager = PluginManager(
        Context(),
        {
            "root": str(tmp_path),
            "officialBundles": ["demo"],
            "npmFileSources": ["https://cdn/"],
        },
    )
    assert (await manager.read_bundle_detail("demo", "en")) == {"status": "none"}
    with pytest.raises(Exception, match="unavailable"):
        await manager.bundle_image("demo", "docs/guide.md", "a.png")
    assert any("demo@1.2.3" in url for url in urls)
    assert any("/1.2.3/files/" in url for url in urls)


@pytest.mark.asyncio
async def test_list_bundles_ignores_invalid_install_target(tmp_path: Path) -> None:
    anchor = tmp_path / "package.json"
    anchor.write_text(
        json.dumps(
            {
                "name": "@deepseek-ai/dsh",
                "dependencies": {"dsh": "workspace:*"},
            }
        ),
        encoding="utf-8",
    )
    manager = PluginManager(
        Context(),
        {
            "root": str(tmp_path),
            "officialBundles": ["demo"],
            "installAnchor": str(anchor),
        },
    )
    rows = await manager.list_bundles()
    assert "installTarget" not in rows[0]
