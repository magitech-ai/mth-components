"""Unit tests for skills filesystem provider service."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from animas import Context

from mth_components.bridges.skills_filesystem.service import (
    SkillsFilesystemProviderService,
    _parse_bool,
    _parse_skill_frontmatter,
)
from mth_components.bridges.skills_registry.service import SkillsProviderRegistryService


def _skill(root: Path, name: str, extra: str = "") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: desc {name}\n{extra}---\n\nBody of {name}.\n",
        encoding="utf-8",
    )
    return d


def _setup(ctx: Context) -> None:
    ctx.use(SkillsProviderRegistryService)
    ctx.use(SkillsFilesystemProviderService)


def test_parse_bool() -> None:
    assert _parse_bool(None, False) is False
    assert _parse_bool(None, True) is True
    assert _parse_bool("true", False) is True
    assert _parse_bool(" FALSE ", True) is False
    assert _parse_bool("yes", True) is None


def test_parse_frontmatter_valid() -> None:
    result = _parse_skill_frontmatter(
        "---\nname: x\ndescription: d\ndisable-model-invocation: true\n---\n\nbody"
    )
    assert result is not None
    assert result["name"] == "x"
    assert result["description"] == "d"
    assert result["body"] == "body"
    assert result["invocation"].model_invocable is False
    assert result["invocation"].user_invocable is True


def test_parse_frontmatter_user_invocable_false() -> None:
    result = _parse_skill_frontmatter(
        "---\nname: x\ndescription: d\nuser-invocable: false\n---\n"
    )
    assert result is not None
    assert result["invocation"].user_invocable is False


def test_parse_frontmatter_invalid_spelling_drops() -> None:
    assert (
        _parse_skill_frontmatter(
            "---\nname: x\ndescription: d\ndisable-model-invocation: maybe\n---\n"
        )
        is None
    )


def test_parse_frontmatter_missing_fields() -> None:
    assert _parse_skill_frontmatter("no frontmatter") is None
    assert _parse_skill_frontmatter("---\nname: x\n---\n") is None  # no description
    assert _parse_skill_frontmatter("---\ndescription: d\n---\n") is None  # no name
    assert _parse_skill_frontmatter("---\nno closing") is None


async def test_discover_with_scope_precedence(tmp_path: Path) -> None:
    ctx = Context()
    _setup(ctx)
    await ctx.flush()
    svc = ctx.skills_filesystem
    user = tmp_path / "user"
    home = tmp_path / "home"
    _skill(user, "dup")
    _skill(home, "dup")
    _skill(home, "home-only")
    discovered = svc.discover_skills(roots=[(home, "home"), (user, "user")])
    assert "dup" in discovered
    assert "home-only" in discovered
    registry = ctx.skills_registry
    # home (rank 1) beats user (rank 2)
    assert registry.get_skill("dup").path.parent == home


async def test_discover_symlink_dedup(tmp_path: Path) -> None:
    ctx = Context()
    _setup(ctx)
    await ctx.flush()
    svc = ctx.skills_filesystem
    real = tmp_path / "real"
    alias = tmp_path / "alias"
    real.mkdir()
    alias.mkdir()
    _skill(real, "s")
    (alias / "s").symlink_to(real / "s", target_is_directory=True)
    discovered = svc.discover_skills(roots=[(real, "home"), (alias, "user")])
    assert discovered.count("s") == 1


async def test_discover_skips_invalid(tmp_path: Path) -> None:
    ctx = Context()
    _setup(ctx)
    await ctx.flush()
    svc = ctx.skills_filesystem
    (tmp_path / "noindex").mkdir()
    (tmp_path / "afile").write_text("x")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("not frontmatter")
    _skill(tmp_path, "unreadable")
    assert svc.discover_skills(roots=[(tmp_path, "home")]) == ["unreadable"]


async def test_discover_no_roots() -> None:
    ctx = Context()
    _setup(ctx)
    await ctx.flush()
    svc = ctx.skills_filesystem
    assert svc.discover_skills(roots=[]) == []


async def test_discover_default_root(tmp_path: Path, monkeypatch) -> None:
    ctx = Context()
    _setup(ctx)
    await ctx.flush()
    svc = ctx.skills_filesystem
    monkeypatch.chdir(tmp_path)
    result = svc.discover_skills()
    assert isinstance(result, list)


def test_cleanup() -> None:
    ctx = Context()
    _setup(ctx)
    svc = ctx.skills_filesystem
    svc._discovered = ["x"]
    svc._cleanup()
    assert svc._discovered == []


def test_parse_frontmatter_skips_lines() -> None:
    result = _parse_skill_frontmatter(
        "---\n# a comment\n\nno colon here\nname: x\ndescription: d\n---\n"
    )
    assert result is not None
    assert result["name"] == "x"


async def test_discover_unreadable_skill(tmp_path: Path) -> None:
    ctx = Context()
    _setup(ctx)
    await ctx.flush()
    svc = ctx.skills_filesystem
    _skill(tmp_path, "unreadable")
    with patch.object(Path, "read_text", side_effect=OSError("denied")):
        assert svc.discover_skills(roots=[(tmp_path, "home")]) == []


async def test_discover_duplicate_register_loss(tmp_path: Path) -> None:
    ctx = Context()
    _setup(ctx)
    await ctx.flush()
    svc = ctx.skills_filesystem
    _skill(tmp_path, "dup")
    assert svc.discover_skills(roots=[(tmp_path, "home")]) == ["dup"]
    # Second discovery: registry rejects duplicate tie -> not in discovered.
    assert svc.discover_skills(roots=[(tmp_path, "home")]) == []
