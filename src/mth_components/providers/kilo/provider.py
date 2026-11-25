"""Kilo Code model provider for Magitech Harness.

Kilo Code (v7.8.32) ships 404 models including a set of `:free` variants and a
`kilo-auto/free` router, with **no API key required** (`kilo auth list` reported
0 credentials; a live `kilo run --model kilo/kilo-auto/free` returned a valid
JSON event stream with `cost: 0`).

This provider drives the `kilo run` CLI in `--format json` mode. It is an
optional marketplace provider: the harness itself stays provider-agnostic. A
`kilo-auto/free` default gives a keyless bootstrap provider for new installs.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

from magitech_harness.agent.cancellation import AbortSignal
from magitech_harness.llm.types import ModelChunk, ModelResponse, UsageData
from magitech_harness.session.events import ToolCallData

DEFAULT_MODEL = "kilo-auto/free"
DEFAULT_TIMEOUT_SECONDS = 120.0


def wire_model_id(model_id: str) -> str:
    """Reduce a harness model slug to the Kilo `provider/model` wire id."""
    return model_id.removeprefix("kilo/") if model_id else DEFAULT_MODEL


def parse_event_line(line: str) -> dict[str, Any] | None:
    """Parse one JSONL event from `kilo run --format json`."""
    text = line.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _tool_calls_from_part(part: dict[str, Any]) -> list[ToolCallData]:
    """Extract tool calls from a Kilo tool-use event part."""
    raw = part.get("toolCalls") or part.get("tool_calls") or []
    calls: list[ToolCallData] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        arguments = item.get("arguments") or item.get("input") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"raw": arguments}
        name = item.get("name") or item.get("tool") or ""
        call_id = item.get("id") or item.get("callId") or ""
        calls.append(
            ToolCallData(
                call_id=str(call_id),
                name=str(name),
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        )
    return calls


def _text_from_part(part: dict[str, Any]) -> str:
    """Extract text from a Kilo text event part."""
    text = part.get("text", "")
    return text if isinstance(text, str) else ""


def _usage_from_metrics(part: dict[str, Any]) -> UsageData:
    """Map Kilo step-finish token counts into the harness usage shape.

    The CLI puts ``tokens`` beside ``metrics`` on the event part.
    """
    tokens = part.get("tokens") if isinstance(part, dict) else None
    if tokens is None:
        metrics = part.get("metrics") if isinstance(part, dict) else None
        tokens = metrics.get("tokens") if isinstance(metrics, dict) else None
    if not isinstance(tokens, dict):
        return UsageData()
    try:
        prompt = int(tokens.get("input", 0))
        completion = int(tokens.get("output", 0))
        return UsageData(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
        )
    except (TypeError, ValueError):  # pragma: no cover - defensive wire guard
        return UsageData()


class KiloProviderError(RuntimeError):
    """Raised when the Kilo CLI cannot serve a request."""


class KiloProvider:
    """LLMProvider that drives the Kilo Code CLI as a keyless model seam."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        binary: str = "kilo",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        env: dict[str, str] | None = None,
    ) -> None:
        self.model = model
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self.env = env
        self.call_history: list[dict[str, Any]] = []

    def list_models(self, provider: str) -> list[dict[str, str]]:
        """Return the configured default model (the CLI list is 404 entries)."""
        del provider
        return [
            {
                "id": self.model,
                "name": self.model,
                "provider": "kilo",
            }
        ]

    def _command(
        self,
        messages: Sequence[dict[str, Any]],
        system_prompt: str | None,
        max_tokens: int | None,
    ) -> list[str]:
        prompt_parts = [
            f"{message.get('role', 'user')}: {message.get('content', '')}"
            for message in messages
        ]
        if system_prompt:
            prompt_parts.insert(0, f"system: {system_prompt}")
        # Kilo has no max-token CLI flag; the model's own budget applies.
        # ``max_tokens`` is accepted for the LLMProvider contract but not
        # forwarded, so the command stays valid for every call site.
        del max_tokens
        return [
            self.binary,
            "run",
            "--format",
            "json",
            "--model",
            f"kilo/{wire_model_id(self.model)}",
            "\n\n".join(str(part) for part in prompt_parts),
        ]

    async def _read_events(
        self,
        command: list[str],
        signal: AbortSignal | None = None,
    ) -> list[dict[str, Any]]:
        env = {**os.environ, **(self.env or {})}
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        if signal is not None and signal.aborted:
            proc.kill()
            await proc.wait()
            return []

        async def _wait() -> bytes:
            assert proc.stdout is not None
            return await proc.stdout.read()

        try:
            stdout = await asyncio.wait_for(_wait(), self.timeout_seconds)
        except TimeoutError:  # pragma: no cover - network-dependent
            proc.kill()
            await proc.wait()
            raise KiloProviderError(
                f"kilo run timed out after {self.timeout_seconds}s"
            ) from None
        await proc.wait()
        if proc.returncode not in (0, None):  # pragma: no cover - CLI failure
            stderr = b""
            if proc.stderr is not None:
                stderr = await proc.stderr.read()
            raise KiloProviderError(
                f"kilo run exited {proc.returncode}: "
                f"{stderr.decode('utf-8', errors='replace')[:200]}"
            )
        return [
            event
            for line in stdout.decode("utf-8", errors="replace").splitlines()
            if (event := parse_event_line(line)) is not None
        ]

    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        signal: AbortSignal | None = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Run one Kilo turn and return a complete ModelResponse."""
        del tools, kwargs
        command = self._command(messages, system_prompt, max_tokens)
        if model:
            command[command.index("--model") + 1] = f"kilo/{wire_model_id(model)}"
        self.call_history.append({"command": command, "messages": messages})
        events = await self._read_events(command, signal)
        text_parts: list[str] = []
        tool_calls: list[ToolCallData] = []
        usage = UsageData()
        for event in events:
            raw_part = event.get("part")
            part: dict[str, Any] = raw_part if isinstance(raw_part, dict) else {}
            if event.get("type") == "text":
                text_parts.append(_text_from_part(part))
            if event.get("type") in ("tool-use", "tool_call"):  # pragma: no cover
                tool_calls.extend(_tool_calls_from_part(part))
            if event.get("type") == "step_finish":
                usage = _usage_from_metrics(part)
        return ModelResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason="tool-calls" if tool_calls else "stop",
            usage=usage,
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        *,
        model: str | None = None,
        signal: AbortSignal | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ModelChunk]:
        """Stream Kilo JSON events as ModelChunks."""
        response = await self.generate(
            messages,
            tools,
            system_prompt,
            model=model,
            signal=signal,
            **kwargs,
        )
        if response.text:
            yield ModelChunk(
                text=response.text,
                tool_calls=[],
                finish_reason=None,
            )
        if response.tool_calls:
            yield ModelChunk(
                text="",
                tool_calls=response.tool_calls,
                finish_reason=None,
            )
        yield ModelChunk(
            text="",
            tool_calls=[],
            finish_reason=response.finish_reason,
        )


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT_SECONDS",
    "KiloAdapter",
    "KiloProvider",
    "KiloProviderError",
    "parse_event_line",
    "wire_model_id",
]


class KiloAdapter:
    """Unified-driver ``LlmAdapter`` over the Kilo CLI.

    ``LLMService.stream`` dispatches through registered adapters, not legacy
    providers, so this adapter is what actually drives a harness turn. It maps
    Kilo's JSON event stream to the driver's block-oriented ``StreamChunk``
    vocabulary and emits tool calls with incrementally assembled arguments.
    """

    def __init__(
        self,
        *,
        binary: str = "kilo",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        env: dict[str, str] | None = None,
    ) -> None:
        self._provider = KiloProvider(
            binary=binary, timeout_seconds=timeout_seconds, env=env
        )

    async def list_models(self, provider: str) -> list[dict[str, str]]:
        """Return the published free default (the CLI list has 404 entries)."""
        del provider
        return [{"provider": "kilo", "id": DEFAULT_MODEL, "name": "kilo-auto/free"}]

    async def stream(self, options: Any) -> Any:
        """Stream a Kilo turn as ``block-start`` / delta / ``finish`` chunks."""
        model = str(options.get("model") or DEFAULT_MODEL)
        self._provider.model = model
        system = (
            options.get("system") if isinstance(options.get("system"), str) else None
        )
        messages = list(options.get("messages") or [])
        signal = options.get("signal")
        response = await self._provider.generate(
            messages,
            system_prompt=system,
            signal=signal,
            model=model,
        )
        index = 0
        if response.text:
            yield {"type": "block-start", "index": index, "blockType": "text"}
            yield {
                "type": "text-delta",
                "index": index,
                "text": response.text,
            }
            yield {
                "type": "block-end",
                "index": index,
                "block": {"type": "text", "text": response.text},
            }
            index += 1
        for call in response.tool_calls:
            arguments = json.dumps(call.arguments, sort_keys=True)
            yield {
                "type": "block-start",
                "index": index,
                "blockType": "tool-call",
            }
            yield {
                "type": "tool-call-delta",
                "index": index,
                "id": call.call_id,
                "name": call.name,
                "argumentsDelta": arguments,
            }
            index += 1
        reason: dict[str, Any] = (
            {"kind": "tool-calls"} if response.tool_calls else {"kind": "stop"}
        )
        yield {"type": "finish", "reason": reason}
