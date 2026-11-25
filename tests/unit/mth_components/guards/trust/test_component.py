"""Unit tests for workspace-trust-gate component apply and pre-execute guard."""

from __future__ import annotations

from pathlib import Path

from animas import Context

from mth_components.guards.trust.component import (
    TOOLS_PRE_EXECUTE,
    _pre_execute_handler,
    apply,
)
from mth_components.guards.trust.gate import WorkspaceTrustGate
from mth_components.guards.trust.hasher import compute_directory_tree_hash
from mth_components.guards.trust.store import TrustDecision, TrustStore


class _WorkingDirectory:
    """Minimal working-directory service stub for trust workspace resolution."""

    def __init__(self, directory: str) -> None:
        self.default_directory = directory


async def _pre_execute(ctx: Context, tool_name: str = "read_file") -> dict[str, object]:
    return await ctx.waterfall(
        TOOLS_PRE_EXECUTE,
        {"name": tool_name, "arguments": {}, "callId": "call-1", "signal": None},
        default_handler=lambda _: {"kind": "allow"},
    )


def async_next(result: dict[str, object]):
    """Return an async next_fn that returns *result*."""

    async def _next(_exec_view: dict[str, object]) -> dict[str, object]:
        return result

    return _next


async def test_trust_component_apply(tmp_path: Path) -> None:
    ctx = Context()
    apply(ctx, store_path=tmp_path / "trust.json")
    await ctx.flush()

    assert ctx.has("trust")
    gate: WorkspaceTrustGate = ctx.trust
    assert gate.store.file_path == tmp_path / "trust.json"


async def test_pre_execute_allows_workspace_without_agents_dir(tmp_path: Path) -> None:
    ctx = Context()
    ctx._register_service("workingDirectory", _WorkingDirectory(str(tmp_path)))
    apply(ctx, store_path=tmp_path / "trust.json")
    await ctx.flush()

    decision = await _pre_execute(ctx, "read_file")
    assert decision == {"kind": "allow"}


async def test_pre_execute_denies_untrusted_workspace(tmp_path: Path) -> None:
    ctx = Context()
    ctx._register_service("workingDirectory", _WorkingDirectory(str(tmp_path)))
    store = TrustStore(tmp_path / "trust.json")
    apply(ctx, store=store)
    await ctx.flush()

    project_dir = tmp_path / "untrusted"
    agents_dir = project_dir / ".agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "component.json").write_text("{}")
    ctx._register_service("workingDirectory", _WorkingDirectory(str(project_dir)))

    decision = await _pre_execute(ctx, "write_file")
    assert decision["kind"] == "deny"
    assert "not trusted" in decision["reason"]


async def test_pre_execute_allows_trusted_exact_workspace(tmp_path: Path) -> None:
    ctx = Context()
    store = TrustStore(tmp_path / "trust.json")
    apply(ctx, store=store)
    await ctx.flush()

    project_dir = tmp_path / "trusted"
    agents_dir = project_dir / ".agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "component.json").write_text("{}")
    tree_hash = compute_directory_tree_hash(agents_dir)
    store.set_decision(project_dir, TrustDecision.TRUST_EXACT, tree_hash)
    ctx._register_service("workingDirectory", _WorkingDirectory(str(project_dir)))

    decision = await _pre_execute(ctx, "write_file")
    assert decision == {"kind": "allow"}


async def test_pre_execute_allows_trusted_parent_workspace(tmp_path: Path) -> None:
    ctx = Context()
    store = TrustStore(tmp_path / "trust.json")
    apply(ctx, store=store)
    await ctx.flush()

    project_dir = tmp_path / "parent" / "child"
    agents_dir = project_dir / ".agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "component.json").write_text("{}")
    store.add_trusted_parent(tmp_path / "parent")
    ctx._register_service("workingDirectory", _WorkingDirectory(str(project_dir)))

    decision = await _pre_execute(ctx, "write_file")
    assert decision == {"kind": "allow"}


async def test_pre_execute_denies_declined_workspace(tmp_path: Path) -> None:
    ctx = Context()
    store = TrustStore(tmp_path / "trust.json")
    apply(ctx, store=store)
    await ctx.flush()

    project_dir = tmp_path / "declined"
    agents_dir = project_dir / ".agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "component.json").write_text("{}")
    store.set_decision(project_dir, TrustDecision.DECLINE, "sha256:abc")
    ctx._register_service("workingDirectory", _WorkingDirectory(str(project_dir)))

    decision = await _pre_execute(ctx, "read_file")
    assert decision["kind"] == "deny"


async def test_pre_execute_passes_through_when_trusted(tmp_path: Path) -> None:
    ctx = Context()
    ctx._register_service("workingDirectory", _WorkingDirectory(str(tmp_path)))
    apply(ctx, store_path=tmp_path / "trust.json")
    await ctx.flush()

    # next_fn is invoked and its result propagated
    decision = await ctx.waterfall(
        TOOLS_PRE_EXECUTE,
        {"name": "bash", "arguments": {}, "callId": "call-1", "signal": None},
        default_handler=lambda _: {"kind": "allow", "note": "default"},
    )
    assert decision == {"kind": "allow", "note": "default"}


async def test_pre_execute_falls_back_to_cwd_without_valid_directory(
    tmp_path: Path,
) -> None:
    ctx = Context()
    # workingDirectory mounted but with an empty directory: falls back to cwd.
    ctx._register_service("workingDirectory", _WorkingDirectory(""))
    store = TrustStore(tmp_path / "trust.json")
    apply(ctx, store=store)
    await ctx.flush()

    decision = await _pre_execute_handler(
        ctx,
        {"name": "read_file", "arguments": {}, "callId": "call-1", "signal": None},
        async_next({"kind": "allow"}),
    )
    # cwd (repo checkout) has no .agents directory -> trusted.
    assert decision == {"kind": "allow"}


async def test_pre_execute_denies_when_trust_service_absent() -> None:
    ctx = Context()
    ctx._register_service("workingDirectory", _WorkingDirectory(str(Path.cwd())))
    # Trust gate NOT mounted: gate is None -> fail-closed deny.
    decision = await _pre_execute_handler(
        ctx,
        {"name": "read_file", "arguments": {}, "callId": "call-1", "signal": None},
        async_next({"kind": "allow"}),
    )
    assert decision["kind"] == "deny"
    assert "not trusted" in decision["reason"]


async def test_pre_execute_audits_denial(tmp_path: Path) -> None:
    ctx = Context()
    store = TrustStore(tmp_path / "trust.json")
    apply(ctx, store=store)
    await ctx.flush()

    project_dir = tmp_path / "untrusted"
    agents_dir = project_dir / ".agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "component.json").write_text("{}")
    ctx._register_service("workingDirectory", _WorkingDirectory(str(project_dir)))

    audited: list[dict[str, object]] = []
    ctx.on("trust/denied", lambda payload: audited.append(dict(payload)))

    decision = await _pre_execute(ctx, "edit_file")
    assert decision["kind"] == "deny"
    assert len(audited) == 1
    assert audited[0]["tool"] == "edit_file"
    assert audited[0]["workspace"] == str(project_dir)


async def test_pre_execute_without_working_directory_service(tmp_path: Path) -> None:
    ctx = Context()
    store = TrustStore(tmp_path / "trust.json")
    apply(ctx, store=store)
    await ctx.flush()

    # No workingDirectory mounted: falls back to process cwd (no .agents -> trusted)
    decision = await _pre_execute(ctx, "read_file")
    assert decision == {"kind": "allow"}
