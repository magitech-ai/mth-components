"""Tests for the keyless Kilo Code CLI provider."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from animas import Context
from magitech_harness.agent.cancellation import AbortSignal
from magitech_harness.llm.service import LLMService
from magitech_harness.llm.types import ModelChunk, UsageData
from magitech_harness.session.events import ToolCallData

from mth_components.providers.kilo.component import apply
from mth_components.providers.kilo.provider import (
    DEFAULT_MODEL,
    KiloAdapter,
    KiloProvider,
    KiloProviderError,
    _text_from_part,
    _tool_calls_from_part,
    _usage_from_metrics,
    parse_event_line,
    wire_model_id,
)


def test_wire_model_id() -> None:
    assert wire_model_id("kilo/kilo-auto/free") == "kilo-auto/free"
    assert wire_model_id("kilo/z-ai/glm-flash-latest") == "z-ai/glm-flash-latest"
    assert wire_model_id("plain-model") == "plain-model"
    assert wire_model_id("") == DEFAULT_MODEL


def test_parse_event_line() -> None:
    assert parse_event_line("") is None
    assert parse_event_line("not json") is None
    assert parse_event_line('["not an object"]') is None
    line = json.dumps({"type": "text", "part": {"text": "PONG"}})
    assert parse_event_line(line) == {"type": "text", "part": {"text": "PONG"}}


def test_default_model_is_keyless_router() -> None:
    assert DEFAULT_MODEL == "kilo-auto/free"
    assert KiloProvider().model == "kilo-auto/free"


def test_command_shape() -> None:
    provider = KiloProvider()
    command = provider._command([{"role": "user", "content": "Hi"}], "Be brief", 42)
    assert command[:7] == [
        "kilo",
        "run",
        "--format",
        "json",
        "--agent",
        "summary",
        "--model",
    ]
    assert command[7] == "kilo/kilo-auto/free"
    assert "system: Be brief" in command[8]
    assert "user: Hi" in command[8]
    assert "--max-steps" not in command


def test_tool_calls_from_part() -> None:
    assert _tool_calls_from_part({}) == []
    calls = _tool_calls_from_part(
        {
            "toolCalls": [
                {
                    "id": "c1",
                    "name": "read_file",
                    "arguments": '{"path": "a.txt"}',
                }
            ]
        }
    )
    assert len(calls) == 1
    assert isinstance(calls[0], ToolCallData)
    assert calls[0].arguments == {"path": "a.txt"}
    malformed = _tool_calls_from_part(
        {"tool_calls": [{"name": "x", "arguments": "nope"}]}
    )
    assert malformed[0].arguments == {"raw": "nope"}


def test_usage_from_metrics() -> None:
    assert _usage_from_metrics({}) == UsageData()
    assert _usage_from_metrics({"tokens": "nope"}) == UsageData()
    usage = _usage_from_metrics({"tokens": {"input": "10", "output": "4"}})
    assert _usage_from_metrics({"metrics": {"tokens": {"input": 3}}}) == UsageData(
        prompt_tokens=3, total_tokens=3
    )
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 4
    assert _usage_from_metrics({"tokens": {"input": "x"}}) == UsageData()


def test_text_from_part() -> None:
    assert _text_from_part({"text": "hi"}) == "hi"
    assert _text_from_part({"text": 1}) == ""


class _FakeProcess:
    def __init__(self, stdout: bytes, returncode: int = 0) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.returncode = returncode
        self.killed = False

    async def wait(self) -> None:
        return None

    def kill(self) -> None:
        self.killed = True


def _json_events(*events: dict[str, Any]) -> bytes:
    return b"\n".join(json.dumps(event).encode() for event in events) + b"\n"


@pytest.mark.asyncio
async def test_generate_parses_kilo_events() -> None:
    output = _json_events(
        {"type": "step_start", "part": {}},
        {"type": "text", "part": {"text": "PONG2"}},
        {
            "type": "step_finish",
            "part": {
                "metrics": {
                    "tokens": {"input": 100, "output": 4},
                    "cost": 0,
                }
            },
        },
    )
    provider = KiloProvider()
    with patch(
        "mth_components.providers.kilo.provider.asyncio.create_subprocess_exec",
        return_value=_FakeProcess(output),
    ) as spawn:
        result = await provider.generate(
            [{"role": "user", "content": "ping"}],
            model="kilo/kilo-auto/free",
            signal=AbortSignal(),
        )
    assert result.text == "PONG2"
    assert result.finish_reason == "stop"
    assert result.usage.prompt_tokens == 100
    assert result.usage.completion_tokens == 4
    assert spawn.called and provider.call_history
    assert "--agent" in provider.call_history[0]["command"]


@pytest.mark.asyncio
async def test_generate_includes_tool_calls() -> None:
    output = _json_events(
        {
            "type": "tool_use",
            "part": {
                "callID": "c1",
                "tool": "bash",
                "state": {"input": {"command": "echo hi"}},
            },
        },
        {"type": "step_finish", "part": {"reason": "tool-calls"}},
    )
    provider = KiloProvider()
    with patch(
        "mth_components.providers.kilo.provider.asyncio.create_subprocess_exec",
        return_value=_FakeProcess(output),
    ):
        result = await provider.generate([{"role": "user", "content": "run"}])
    assert result.finish_reason == "tool-calls"
    assert result.tool_calls[0].name == "bash"
    assert result.tool_calls[0].arguments == {"command": "echo hi"}


@pytest.mark.asyncio
async def test_generate_aborted_before_dispatch() -> None:
    signal = AbortSignal()
    signal.abort("done")
    process = _FakeProcess(_json_events())
    provider = KiloProvider()
    with patch(
        "mth_components.providers.kilo.provider.asyncio.create_subprocess_exec",
        return_value=process,
    ):
        result = await provider.generate(
            [{"role": "user", "content": "ping"}], signal=signal
        )
    assert result.text == ""
    assert process.killed


@pytest.mark.asyncio
async def test_generate_cli_failure() -> None:
    process = _FakeProcess(b"", returncode=1)
    process.stderr = asyncio.StreamReader()
    process.stderr.feed_data(b"boom")
    process.stderr.feed_eof()
    provider = KiloProvider()
    with (
        patch(
            "mth_components.providers.kilo.provider.asyncio.create_subprocess_exec",
            return_value=process,
        ),
        pytest.raises(KiloProviderError, match="exited 1"),
    ):
        await provider.generate([{"role": "user", "content": "ping"}])


@pytest.mark.asyncio
async def test_stream_yields_three_chunks() -> None:
    output = _json_events(
        {"type": "text", "part": {"text": "hello"}},
        {"type": "step_finish", "part": {"metrics": {"tokens": {"output": 1}}}},
    )
    provider = KiloProvider()
    with patch(
        "mth_components.providers.kilo.provider.asyncio.create_subprocess_exec",
        return_value=_FakeProcess(output),
    ):
        chunks = [
            chunk
            async for chunk in provider.stream([{"role": "user", "content": "ping"}])
        ]
    assert len(chunks) == 3
    assert isinstance(chunks[0], ModelChunk)
    assert chunks[0].text == "hello"
    assert chunks[1].usage.completion_tokens == 1
    assert chunks[2].finish_reason == "stop"


@pytest.mark.asyncio
async def test_list_models() -> None:
    provider = KiloProvider()
    assert provider.list_models("kilo") == [
        {"id": DEFAULT_MODEL, "name": DEFAULT_MODEL, "provider": "kilo"}
    ]


def test_component_mounts_provider() -> None:
    ctx = Context()
    LLMService(ctx)
    apply(ctx, model="kilo/z-ai/glm-flash-latest")
    assert "kilo" in ctx.llm.providers
    assert ctx.llm.providers["kilo"].model == "kilo/z-ai/glm-flash-latest"


@pytest.mark.asyncio
async def test_generate_timeout_kills_process() -> None:
    async def _slow_wait() -> bytes:  # pragma: no cover - exercised via patch
        await asyncio.sleep(10)
        return b""

    provider = KiloProvider(timeout_seconds=0.01)

    async def _read_events(
        _command: list[str], _signal: AbortSignal | None
    ) -> list[dict[str, Any]]:
        raise KiloProviderError("kilo run timed out after 0.01s")

    with (
        patch.object(provider, "_read_events", _read_events),
        pytest.raises(KiloProviderError, match="timed out"),
    ):
        await provider.generate([{"role": "user", "content": "ping"}])


def test_tool_call_part_non_dict_items_are_skipped() -> None:
    """A malformed tool-call array entry is ignored, not fatal."""
    assert _tool_calls_from_part({"toolCalls": ["bad", None]}) == []


@pytest.mark.asyncio
async def test_stream_with_tool_calls_yields_three_chunks() -> None:
    output = _json_events(
        {
            "type": "tool_use",
            "part": {
                "callID": "c1",
                "tool": "bash",
                "state": {"input": {}},
            },
        },
        {
            "type": "step_finish",
            "part": {"reason": "tool-calls", "tokens": {"output": 2}},
        },
    )
    provider = KiloProvider()
    with patch(
        "mth_components.providers.kilo.provider.asyncio.create_subprocess_exec",
        return_value=_FakeProcess(output),
    ):
        chunks = [
            chunk
            async for chunk in provider.stream([{"role": "user", "content": "run"}])
        ]
    assert len(chunks) == 3
    assert chunks[0].tool_calls[0].name == "bash"
    assert chunks[1].usage.completion_tokens == 2
    assert chunks[2].finish_reason == "tool-calls"


@pytest.mark.asyncio
async def test_adapter_lists_and_streams_text() -> None:
    output = _json_events(
        {"type": "text", "part": {"text": "hello"}},
        {"type": "step_finish", "part": {"tokens": {"output": 1}}},
    )
    adapter = KiloAdapter()
    assert (await adapter.list_models("kilo"))[0]["id"] == DEFAULT_MODEL
    with patch(
        "mth_components.providers.kilo.provider.asyncio.create_subprocess_exec",
        return_value=_FakeProcess(output),
    ):
        chunks = [
            chunk
            async for chunk in adapter.stream(
                {"model": "kilo-auto/free", "messages": [], "system": "Be brief"}
            )
        ]
    assert chunks[0]["type"] == "block-start"
    assert chunks[1]["type"] == "text-delta"
    assert chunks[1]["text"] == "hello"
    assert chunks[2]["type"] == "block-end"
    assert chunks[3]["type"] == "usage"
    assert chunks[3]["usage"]["outputTokens"] == 1
    assert chunks[4]["type"] == "finish"
    assert chunks[4]["reason"] == {"kind": "stop"}


@pytest.mark.asyncio
async def test_adapter_streams_tool_calls() -> None:
    output = _json_events(
        {
            "type": "tool_use",
            "part": {
                "callID": "c1",
                "tool": "read_file",
                "state": {"input": {"path": "x"}},
            },
        },
        {
            "type": "step_finish",
            "part": {"reason": "tool-calls", "tokens": {"output": 2}},
        },
    )
    adapter = KiloAdapter()
    with patch(
        "mth_components.providers.kilo.provider.asyncio.create_subprocess_exec",
        return_value=_FakeProcess(output),
    ):
        chunks = [
            chunk
            async for chunk in adapter.stream(
                {"model": "kilo-auto/free", "messages": []}
            )
        ]
    assert chunks[0]["type"] == "block-start"
    assert chunks[0]["blockType"] == "tool-call"
    assert chunks[1]["type"] == "tool-call-delta"
    assert chunks[1]["name"] == "read_file"
    assert json.loads(chunks[1]["argumentsDelta"]) == {"path": "x"}
    assert chunks[2]["type"] == "block-end"
    assert chunks[3]["type"] == "usage"
    assert chunks[4]["reason"] == {"kind": "tool-calls"}


@pytest.mark.asyncio
async def test_generate_midflight_abort_kills_process() -> None:
    """An abort after dispatch kills the child process through the signal."""
    signal = AbortSignal()
    process = _FakeProcess(_json_events({"type": "text", "part": {"text": "x"}}))
    process.returncode = None
    provider = KiloProvider()
    killed: list[bool] = []

    def _on_abort(callback: Any) -> Any:
        killed.append(False)
        callback()  # simulate an abort arriving while the subprocess runs
        return lambda: None

    signal.on_abort = _on_abort  # type: ignore[method-assign]
    with patch(
        "mth_components.providers.kilo.provider.asyncio.create_subprocess_exec",
        return_value=process,
    ):
        await provider.generate([{"role": "user", "content": "ping"}], signal=signal)
    assert killed == [False]


@pytest.mark.asyncio
async def test_midflight_abort_skips_kill_for_settled_process() -> None:
    """A callback fired after the process settled does not kill it again."""
    signal = AbortSignal()
    process = _FakeProcess(_json_events({"type": "text", "part": {"text": "x"}}))
    provider = KiloProvider()

    def _on_abort(callback: Any) -> Any:
        process.returncode = 0
        callback()
        return lambda: None

    signal.on_abort = _on_abort  # type: ignore[method-assign]
    with patch(
        "mth_components.providers.kilo.provider.asyncio.create_subprocess_exec",
        return_value=process,
    ):
        await provider.generate([{"role": "user", "content": "ping"}], signal=signal)
    assert process.returncode == 0


@pytest.mark.asyncio
async def test_real_captured_tool_use_event_is_parsed() -> None:
    """Regression: replay the CLI's actual ``tool_use`` vocabulary."""
    fixture = Path(__file__).resolve().parents[3] / "fixtures" / "kilo-tool.jsonl"
    lines = fixture.read_text(encoding="utf-8").splitlines()
    events = [event for line in lines if (event := parse_event_line(line)) is not None]
    calls: list[Any] = []
    for event in events:
        if event.get("type") == "tool_use":
            calls.extend(_tool_calls_from_part(event.get("part") or {}))
    assert calls
    assert calls[0].name == "read"
    assert calls[0].call_id.startswith("call-")
    assert "filePath" in calls[0].arguments
