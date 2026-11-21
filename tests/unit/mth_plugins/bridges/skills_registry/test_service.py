"""Unit tests for skills provider registry service."""

from __future__ import annotations

from pathlib import Path

from anima import Context

from mth_plugins.bridges.skills_registry.service import (
    SkillBundleInfo,
    SkillInvocation,
    SkillsProviderRegistryService,
)


def _skill(
    name: str, model: bool = True, user: bool = True, rank: int = 0
) -> SkillBundleInfo:
    return SkillBundleInfo(
        name=name,
        description=f"desc {name}",
        path=Path("/tmp") / name,
        skill_file=Path("/tmp") / name / "SKILL.md",
        body=f"body of {name}",
        invocation=SkillInvocation(model_invocable=model, user_invocable=user),
        rank=rank,
    )


def _registry(ctx: Context):
    ctx.use(SkillsProviderRegistryService)
    return ctx.skills_registry


def test_register_first_wins_by_rank() -> None:
    ctx = Context()
    r = _registry(ctx)
    assert r.register_skill(_skill("a", rank=5)) is True
    better = _skill("a", rank=1)
    assert r.register_skill(better) is True
    assert r.get_skill("a") == better
    assert r.register_skill(_skill("a", rank=1)) is False
    assert r.register_skill(_skill("a", rank=9)) is False


def test_get_and_list() -> None:
    ctx = Context()
    r = _registry(ctx)
    r.register_skill(_skill("z"))
    r.register_skill(_skill("a"))
    assert r.get_skill("missing") is None
    assert [s.name for s in r.list_skills()] == ["a", "z"]


def test_invocation_filters() -> None:
    ctx = Context()
    r = _registry(ctx)
    r.register_skill(_skill("model-only", model=True, user=False))
    r.register_skill(_skill("user-only", model=False, user=True))
    r.register_skill(_skill("both", model=True, user=True))
    r.register_skill(_skill("neither", model=False, user=False))
    assert {s.name for s in r.list_model_invocable()} == {"model-only", "both"}
    assert {s.name for s in r.list_user_invocable()} == {"user-only", "both"}


def test_load_for_user() -> None:
    ctx = Context()
    r = _registry(ctx)
    r.register_skill(_skill("usable", user=True))
    r.register_skill(_skill("hidden", user=False))
    assert r.load_for_user("usable") == "body of usable"
    assert "not user-invocable" in r.load_for_user("hidden")
    assert "not found" in r.load_for_user("missing")


def test_cleanup() -> None:
    ctx = Context()
    r = _registry(ctx)
    r.register_skill(_skill("a"))
    assert len(r.skills) == 1
    r._cleanup()
    assert len(r.skills) == 0
