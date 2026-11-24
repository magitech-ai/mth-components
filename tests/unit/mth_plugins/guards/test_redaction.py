"""Unit tests for the tool-output redaction guard."""

from __future__ import annotations

from typing import Any

import pytest
from animas import Context
from magitech_harness.session.events import ToolCallData
from magitech_harness.system_prompt.service import SystemPromptService
from magitech_harness.tools.schema import define_tool
from magitech_harness.tools.service import ToolService

from mth_plugins.guards.redaction.component import apply
from mth_plugins.guards.redaction.redaction import (
    REDACTION_MARKER,
    SECRET_ENV_NAMES,
    redact,
    secret_values,
)


@pytest.fixture(autouse=True)
def _secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-secret-123")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret-456")
    for name in SECRET_ENV_NAMES:
        if name not in ("NVIDIA_API_KEY", "GITHUB_TOKEN"):
            monkeypatch.delenv(name, raising=False)


def test_secret_values_reads_default_names() -> None:
    values = secret_values()
    assert "nv-secret-123" in values
    assert "gh-secret-456" in values


def test_secret_values_honors_explicit_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTOM_TOKEN", "custom-secret")
    assert secret_values(("CUSTOM_TOKEN",)) == ("custom-secret",)


def test_secret_values_skips_unset_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CUSTOM_TOKEN", raising=False)
    assert secret_values(("CUSTOM_TOKEN",)) == ()


def test_redact_replaces_all_known_secrets() -> None:
    text = "key=nv-secret-123 token=gh-secret-456"
    assert redact(text) == f"key={REDACTION_MARKER} token={REDACTION_MARKER}"


def test_redact_with_explicit_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTOM_TOKEN", "custom-secret")
    assert redact("v=custom-secret", ("CUSTOM_TOKEN",)) == f"v={REDACTION_MARKER}"


@pytest.mark.asyncio
async def test_component_redacts_legacy_string_output() -> None:
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    ctx.use(apply)
    await ctx.flush()
    tool = define_tool(
        name="leaky",
        description="Leaks a secret",
        execute=lambda args: "key=nv-secret-123",
    )
    ctx.tools.register(tool)
    result = await ctx.tools.execute(
        ToolCallData(call_id="c1", name="leaky", arguments={})
    )
    assert result.content == f"key={REDACTION_MARKER}"


@pytest.mark.asyncio
async def test_component_redacts_parity_text_blocks() -> None:
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    apply(ctx)
    await ctx.flush()
    result: dict[str, Any] = {
        "content": [
            {"type": "text", "text": "key=nv-secret-123"},
            {"type": "reasoning", "text": "token=gh-secret-456"},
            {"type": "tool-call", "id": "x"},
        ],
        "isError": False,
    }
    decision = await ctx.waterfall(
        "tools/post-execute",
        {"name": "leaky", "callId": "c1"},
        result,
        default_handler=lambda *_a: {"kind": "accept"},
    )
    assert decision["kind"] == "accept"
    assert decision["content"][0]["text"] == f"key={REDACTION_MARKER}"
    assert decision["content"][1]["text"] == f"token={REDACTION_MARKER}"
    assert decision["content"][2] == {"type": "tool-call", "id": "x"}


@pytest.mark.asyncio
async def test_component_passes_non_text_result_through() -> None:
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    apply(ctx)
    await ctx.flush()
    result = {"content": {"count": 42}, "isError": False}
    decision = await ctx.waterfall(
        "tools/post-execute",
        {"name": "obj", "callId": "c2"},
        result,
        default_handler=lambda *_a: {"kind": "accept"},
    )
    assert decision["kind"] == "accept"


@pytest.mark.asyncio
async def test_component_legacy_non_string_result_passthrough() -> None:
    """A string legacy output whose next_fn result is not a string passes through."""
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    apply(ctx)
    await ctx.flush()

    sentinel = {"count": 42}
    decision = await ctx.waterfall(
        "tools/post-execute",
        "raw-string-output",
        sentinel,
        default_handler=lambda *_a: sentinel,
    )
    assert decision is sentinel


@pytest.mark.asyncio
async def test_component_extra_env_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTOM_TOKEN", "custom-secret")
    ctx = Context()
    ctx.use(SystemPromptService)
    ctx.use(ToolService)
    apply(ctx, extra_env_names=("CUSTOM_TOKEN",))
    await ctx.flush()
    tool = define_tool(
        name="leaky2",
        description="Leaks a custom secret",
        execute=lambda args: "v=custom-secret",
    )
    ctx.tools.register(tool)
    result = await ctx.tools.execute(
        ToolCallData(call_id="c3", name="leaky2", arguments={})
    )
    assert result.content == f"v={REDACTION_MARKER}"


def test_apply_inject_declares_tools() -> None:
    assert apply.inject == ["tools"]
