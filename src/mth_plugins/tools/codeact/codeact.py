"""CodeAct tool component package's execution engine (subprocess PTC runtime)."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from anima import Context
from magitech_harness.tools.service import ToolCallData

from mth_plugins.tools.codeact.transform import (
    transform_code,
)

# Only these environment variables cross the subprocess boundary. Secrets
# (API keys, tokens) are never inherited.
_SAFE_ENV_KEYS = (
    "PATH",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "PYTHONIOENCODING",
)
DEFAULT_TIMEOUT_S = 30.0


class CodeActExecutor:
    """CodeAct executor backed by a subprocess PTC runtime."""

    def __init__(self, ctx: Context, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.ctx = ctx
        self.session_locals: dict[str, Any] = {}
        self._proc: subprocess.Popen[str] | None = None
        self._timeout_s = timeout_s
        self._tmp_dir: tempfile.TemporaryDirectory[str] | None = None

    def get_registered_tool_names(self) -> set[str]:
        tools_svc = getattr(self.ctx, "tools", None)
        if tools_svc is None:
            return set()
        registry = getattr(tools_svc, "_registry", None)
        if isinstance(registry, dict):
            names = set(registry.keys())
        elif hasattr(tools_svc, "definitions"):
            names = {d["name"] for d in tools_svc.definitions()}
        else:
            all_tools = getattr(tools_svc, "list_all", None)
            names = (
                {(t["name"] if isinstance(t, dict) else t.name) for t in all_tools()}
                if callable(all_tools)
                else set()
            )
        names.discard("execute_python")
        return names

    async def dispatch_tool(self, tool_name: str, *args: Any, **kwargs: Any) -> Any:
        tools_svc = getattr(self.ctx, "tools", None)
        if tools_svc is None:
            raise RuntimeError(f"ToolService not mounted; cannot invoke '{tool_name}'")

        tool = tools_svc.get(tool_name)
        if tool is None:
            raise NameError(f"Tool '{tool_name}' is not registered.")

        merged_kwargs = dict(kwargs)
        if args:  # pragma: no cover
            parameters = tool.get("parameters", {}) if isinstance(tool, dict) else {}
            properties = parameters.get("properties") or parameters or {}
            param_names = list(properties.keys())
            for i, arg in enumerate(args):
                if i < len(param_names):
                    merged_kwargs[param_names[i]] = arg

        call_id = f"codeact_{uuid.uuid4().hex[:8]}"
        call_data = ToolCallData(
            call_id=call_id, name=tool_name, arguments=merged_kwargs
        )
        result = await tools_svc.execute(call_data)
        if getattr(result, "is_error", False):
            raise RuntimeError(f"Tool '{tool_name}' failed: {result.content}")
        content = getattr(result, "content", result)
        if hasattr(content, "is_error") and content.is_error:
            raise RuntimeError(f"Tool '{tool_name}' failed: {content.content}")
        return content

    def transform_code(  # pragma: no cover
        self, code_str: str, tool_names: set[str]
    ) -> tuple[ast.Module, set[str]]:
        return transform_code(code_str, tool_names)

    def _minimal_env(
        self, src_dir: str
    ) -> tuple[dict[str, str], tempfile.TemporaryDirectory[str]]:
        """Build a scrubbed environment for the runner subprocess."""
        tmp = tempfile.TemporaryDirectory(prefix="codeact-")
        env = {key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ}
        env["PYTHONPATH"] = src_dir
        env["HOME"] = tmp.name
        env["USERPROFILE"] = tmp.name
        return env, tmp

    def _start_proc(self) -> None:
        src_dir = str(Path(__file__).resolve().parents[3])
        env, tmp = self._minimal_env(src_dir)
        self._tmp_dir = tmp
        runner_path = str(Path(__file__).resolve().parent / "runner.py")
        self._proc = subprocess.Popen(
            [sys.executable, runner_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
            cwd=tmp.name,
        )

    def _kill_proc(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
        self._proc = None
        if self._tmp_dir is not None:
            self._tmp_dir.cleanup()
            self._tmp_dir = None

    def dispose(self) -> None:
        """Terminate the runner and release its scratch directory."""
        self._kill_proc()

    def __del__(self) -> None:  # pragma: no cover - defensive finalizer
        try:
            self.dispose()
        except Exception:
            pass

    async def execute(self, code: str) -> str:  # noqa: PLR0915
        tool_names = self.get_registered_tool_names()

        if self._proc is None or self._proc.poll() is not None:
            self._start_proc()

        proc = self._proc
        assert proc is not None
        stdin = proc.stdin
        stdout = proc.stdout
        assert stdin is not None
        assert stdout is not None

        payload = {"code": code, "tool_names": sorted(tool_names)}
        stdin.write(json.dumps(payload) + "\n")
        stdin.flush()

        result_value: str | None = None
        error_message: str | None = None

        async def _read_loop() -> None:  # noqa: PLR0912 - IPC message dispatch
            nonlocal result_value, error_message
            while True:
                line = await asyncio.to_thread(stdout.readline)
                if not line:  # pragma: no cover
                    break
                line = line.strip()
                if not line:  # pragma: no cover
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:  # pragma: no cover
                    continue
                msg_type = msg.get("type")
                if msg_type == "tool_call":
                    tool_name = str(msg.get("name", ""))
                    args = msg.get("args") or []
                    kwargs = msg.get("kwargs") or {}
                    try:
                        merged_kwargs = dict(kwargs)
                        if isinstance(args, list) and args:  # pragma: no cover
                            tools_svc = getattr(self.ctx, "tools", None)
                            tool = (
                                tools_svc.get(tool_name)
                                if tools_svc is not None
                                else None
                            )
                            if tool is not None:  # pragma: no cover
                                parameters = (
                                    tool.get("parameters", {})
                                    if isinstance(tool, dict)
                                    else {}
                                )
                                properties = (
                                    parameters.get("properties") or parameters or {}
                                )
                                param_names = list(properties.keys())
                                for i, arg in enumerate(args):
                                    if i < len(param_names):
                                        merged_kwargs[param_names[i]] = arg
                        result = await self.dispatch_tool(tool_name, **merged_kwargs)
                        try:
                            payload_out = json.dumps({"result": result})
                        except (TypeError, ValueError):  # pragma: no cover
                            payload_out = json.dumps({"result": str(result)})
                        stdin.write(payload_out + "\n")
                        stdin.flush()
                    except Exception as exc:
                        stdin.write(json.dumps({"error": str(exc)}) + "\n")
                        stdin.flush()
                elif msg_type == "result":
                    result_value = str(msg.get("value", ""))
                    names = msg.get("session_local_names") or []
                    vals = msg.get("session_locals") or {}
                    for name in names:
                        self.session_locals[name] = vals.get(name)
                    return
                elif msg_type == "error":  # pragma: no cover
                    error_message = str(msg.get("message", "Execution Error"))
                    return  # pragma: no cover

        try:
            await asyncio.wait_for(_read_loop(), timeout=self._timeout_s)
        except TimeoutError:
            self._kill_proc()
            return f"Error: CodeAct execution timed out after {self._timeout_s}s"
        except Exception as exc:
            error_message = f"Execution Error: {exc}"

        if error_message is not None:  # pragma: no cover
            return error_message
        if result_value is not None:
            return result_value
        # A clean exit without a result is a failure, never a success.
        return "Error: CodeAct execution produced no result"
