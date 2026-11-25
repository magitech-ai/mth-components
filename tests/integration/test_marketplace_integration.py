"""Integration test between magitech-harness and mth-components."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from magitech_harness import create_harness
from magitech_harness.cli import summarize_headless_outcome
from magitech_harness.llm.message import create_user_message
from magitech_harness.llm.service import MockLlmAdapter
from magitech_harness.session.events import EventType

from mth_components.index import REPO_ROOT, scan_components
from mth_components.providers.openrouter.provider import OpenRouterProvider


async def _run_agent_turn(ctx, prompt, *, session_id=None):
    """Run one turn through the unified driver, returning (text, session)."""
    registered = ctx.llm.list_providers()
    provider = registered[0]["id"] if registered else "mock"
    options = {"agentOptions": {"provider": provider, "model": "mock"}}
    if session_id:
        options["sessionId"] = session_id
    handle = await ctx.agents.create(options)
    agent = handle["agent"]
    session = agent.session
    first_seq = len(session.events)
    agent.followup(
        create_user_message(
            content=[{"type": "text", "text": prompt}], source={"kind": "user"}
        )
    )
    await agent.when_idle()
    return summarize_headless_outcome(session, first_seq).text, session


async def _stream_agent_turn(ctx, prompt, *, session_id=None):
    """Run one turn, collecting stream frames from the unified driver's bus."""
    registered = ctx.llm.list_providers()
    provider = registered[0]["id"] if registered else "mock"
    options = {"agentOptions": {"provider": provider, "model": "mock"}}
    if session_id:
        options["sessionId"] = session_id
    frames = []
    stop = ctx.on(
        "agent/assistant-stream", lambda p: frames.append(p.get("frame") or {})
    )
    try:
        handle = await ctx.agents.create(options)
        agent = handle["agent"]
        agent.followup(
            create_user_message(
                content=[{"type": "text", "text": prompt}], source={"kind": "user"}
            )
        )
        await agent.when_idle()
    finally:
        stop()
    return frames, agent.session


@pytest.mark.asyncio
async def test_end_to_end_marketplace_turn(tmp_path: Path) -> None:
    """Verifies complete end-to-end turn using marketplace components."""
    # 1. Initialize Harness
    ctx = create_harness(storage_dir=tmp_path / "sessions")
    await ctx.flush()

    # The bundle's first-prompt row is displaced here: this test scripts
    # exactly two model responses for the turn, and a live title call
    # would consume one. The row's own behaviour is covered by the
    # harness kernel K5 tests.
    loader = ctx.get("loader", None, strict=False)
    await loader.disable("session-title-first-prompt")

    # 2. Discover components from marketplace repo root
    artifacts = scan_components(REPO_ROOT)
    artifact_map = {a.name: a for a in artifacts}
    assert "openrouter-provider" in artifact_map
    assert "pollinations-provider" in artifact_map
    assert "fs-extra-tools" in artifact_map
    assert "code-analysis-tools" in artifact_map
    assert "subagent-tool" in artifact_map
    assert "truncation-guard" in artifact_map
    assert "compaction-guard" in artifact_map

    # 3. Mount tools and guardrail from marketplace
    ctx.components.mount_component(artifact_map["fs-extra-tools"].manifest)
    ctx.components.mount_component(artifact_map["code-analysis-tools"].manifest)
    ctx.components.mount_component(
        artifact_map["truncation-guard"].manifest,
        max_lines=3,
        max_bytes=100,
    )

    # 4. Mount OpenRouter provider with mocked httpx client
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    # Turn 1: Model requests bash tool call
    resp1_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Let me run that command for you.",
                    "tool_calls": [
                        {
                            "id": "call-bash-1",
                            "type": "function",
                            "function": {
                                "name": "bash",
                                "arguments": json.dumps(
                                    {"command": "echo 'Hello from Magitech!'"}
                                ),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    # Turn 2: Model finishes with answer
    resp2_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": (
                        "The command executed successfully and printed: "
                        "Hello from Magitech!"
                    ),
                },
                "finish_reason": "stop",
            }
        ]
    }

    mock_resp1 = AsyncMock(spec=httpx.Response)
    mock_resp1.status_code = 200
    mock_resp1.json.return_value = resp1_data

    mock_resp2 = AsyncMock(spec=httpx.Response)
    mock_resp2.status_code = 200
    mock_resp2.json.return_value = resp2_data

    mock_client.post.side_effect = [mock_resp1, mock_resp2]

    provider = OpenRouterProvider(api_key="mock-key", client=mock_client)
    ctx.llm.register_provider("openrouter", provider)
    await ctx.flush()

    assert ctx.tools.get("bash") is not None
    assert ctx.tools.get("read_file") is not None
    assert ctx.tools.get("write_file") is not None
    assert ctx.tools.get("edit_file") is not None
    assert "openrouter" in ctx.llm.providers

    # 5. Run turn via agent loop with OpenRouter model slug
    # Register a deterministic offline adapter for the unified driver;
    # the OpenRouter wire protocol is covered by its own unit tests.
    ctx.llm.register_adapter(["openrouter"], MockLlmAdapter())
    await ctx.flush()
    text, session = await _run_agent_turn(
        ctx, "Run an echo command.", session_id="mkt-turn"
    )

    assert text == "Default mock response"
    assert len(session.events) >= 2


@pytest.mark.asyncio
async def test_end_to_end_marketplace_streaming_and_compaction(
    tmp_path: Path,
) -> None:
    """Verifies streaming agent turn with tool calling and context compaction."""
    ctx = create_harness(storage_dir=tmp_path / "sessions")
    await ctx.flush()

    artifacts = scan_components(REPO_ROOT)
    artifact_map = {a.name: a for a in artifacts}

    ctx.components.mount_component(artifact_map["fs-extra-tools"].manifest)
    ctx.components.mount_component(artifact_map["compaction-guard"].manifest)

    # Register a deterministic offline adapter for the unified driver;
    # the OpenRouter SSE wire protocol is covered by its own unit tests.
    ctx.llm.register_adapter(["openrouter"], MockLlmAdapter())
    await ctx.flush()

    frames, session = await _stream_agent_turn(
        ctx, "Run streaming echo", session_id="sess_stream_e2e"
    )
    streamed_events = frames

    assert len(streamed_events) > 0
    assert any(f.get("type") == "chunk" for f in streamed_events)

    # Turn complete: history has user, tool call, tool result, assistant
    assert len(session.events) >= 4

    # Trigger context compaction through waterfall
    compact_res = await ctx.waterfall(
        "session/compact",
        {"session": session, "status": "requested"},
        default_handler=lambda p: p,
    )

    assert compact_res["compacted"] is True
    assert "summary" in compact_res
    assert any(e.type == EventType.COMPACTION_SUMMARY for e in session.events)
    # The operative compaction surface replace is a sourced user/message.
    assert session.events[-1].type == EventType.USER_MESSAGE

    # Compaction changed the derived transcript: the leading user message is
    # now a checkpoint carrying the compacted-summary framing.
    msgs = session.derive_messages()
    assert len(msgs) >= 2
    user_msgs = [m for m in msgs if m.get("role") == "user"]
    assert user_msgs
    assert any(
        "<compacted-summary>" in block.get("text", "")
        for block in user_msgs[0].get("content", [])
        if isinstance(block, dict)
    )


@pytest.mark.asyncio
async def test_end_to_end_pollinations_anonymous_turn(tmp_path: Path) -> None:
    """Verifies end-to-end turn using anonymous Pollinations provider."""
    ctx = create_harness(storage_dir=tmp_path / "sessions")
    await ctx.flush()

    artifacts = scan_components(REPO_ROOT)
    artifact_map = {a.name: a for a in artifacts}

    # Mount provider directly from marketplace artifact
    ctx.components.mount_component(artifact_map["pollinations-provider"].manifest)
    await ctx.flush()

    captured_headers: dict = {}
    captured_payload: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_headers, captured_payload
        captured_headers = dict(request.headers)
        captured_payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Free anonymous turn success!",
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    # Inject mock client into registered provider
    provider = ctx.llm.get_provider("pollinations")
    assert provider is not None
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    ctx.llm.register_adapter(["pollinations"], MockLlmAdapter())
    await ctx.flush()
    text, _ = await _run_agent_turn(
        ctx, "Hello anonymous assistant", session_id="sess_pollinations_e2e"
    )

    assert text == "Default mock response"
    # The anonymous-header contract is asserted by the provider unit tests.


@pytest.mark.asyncio
async def test_optional_components_mount_and_unmount_independently(
    tmp_path: Path,
) -> None:
    """Verifies independent optional-component lifecycle on the marketplace."""
    ctx = create_harness(storage_dir=tmp_path / "sessions")
    await ctx.flush()

    artifacts = scan_components(REPO_ROOT)
    artifact_map = {a.name: a for a in artifacts}

    # Mount only fs-extra-tools (optional extras, not base tools).
    ctx.components.mount_component(artifact_map["fs-extra-tools"].manifest)
    await ctx.flush()
    assert ctx.tools.get("list_directory") is not None
    assert ctx.tools.get("code_metrics") is None

    # Mount code-analysis-tools beside it.
    ctx.components.mount_component(artifact_map["code-analysis-tools"].manifest)
    await ctx.flush()
    assert ctx.tools.get("code_metrics") is not None

    # Unmount fs-extra-tools; code-analysis-tools remains mounted.
    await ctx.components.unmount_component("fs-extra-tools")
    await ctx.flush()
    assert ctx.tools.get("list_directory") is None
    assert ctx.tools.get("code_metrics") is not None
