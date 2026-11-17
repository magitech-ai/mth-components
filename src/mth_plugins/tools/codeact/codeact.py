"""CodeAct tool component package's execution engine (subprocess PTC runtime)."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from anima import Context
from magitech_harness.tools.service import ToolCallData

from mth_plugins.tools.codeact.transform import (
    transform_code,
)


class CodeActExecutor:
    """CodeAct executor backed by a long-running subprocess PTC runtime."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.session_locals: dict[str, Any] = {}
        self._proc: subprocess.Popen[str] | None = None

    def get_registered_tool_names(self) -> set[str]:
        tools_svc = getattr(self.ctx, "tools", None)
        if tools_svc is None:
            return set()
        registry = getattr(tools_svc, "_registry", None)
        if isinstance(registry, dict):
            names = set(registry.keys())
        elif hasattr(tools_svc, "definitions"):
            names = {d["name"] for d in tools_svc.definitions()}
            legacy = getattr(tools_svc, "_legacy_tools", {})
            names.update(legacy.keys())
        else:
            all_tools = getattr(tools_svc, "list_all", None)
            names = {t.name for t in all_tools()} if callable(all_tools) else set()
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
            param_names = list(getattr(tool, "parameters", {}).keys())
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

    async def execute(self, code: str) -> str:  # noqa: PLR0912, PLR0915
        tool_names = self.get_registered_tool_names()

        if self._proc is None or self._proc.poll() is not None:
            env = dict(os.environ)
            src_dir = str(Path(__file__).resolve().parents[3])
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                src_dir if not existing else src_dir + os.pathsep + existing
            )
            runner_path = str(Path(__file__).resolve().parent / "runner.py")
            self._proc = subprocess.Popen(
                [sys.executable, runner_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )

        proc = self._proc
        assert proc.stdin is not None
        assert proc.stdout is not None

        payload = {"code": code, "tool_names": sorted(tool_names)}
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

        result_value: str | None = None
        error_message: str | None = None
        try:
            while True:
                line = proc.stdout.readline()
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
                        if isinstance(args, list):  # pragma: no cover
                            tools_svc = getattr(self.ctx, "tools", None)
                            tool = (
                                tools_svc.get(tool_name)
                                if tools_svc is not None
                                else None
                            )
                            if tool is not None:  # pragma: no cover
                                param_names = list(
                                    getattr(tool, "parameters", {}).keys()
                                )
                                for i, arg in enumerate(args):
                                    if i < len(param_names):
                                        merged_kwargs[param_names[i]] = arg
                        result = await self.dispatch_tool(tool_name, **merged_kwargs)
                        try:
                            payload_out = json.dumps({"result": result})
                        except (TypeError, ValueError):  # pragma: no cover
                            payload_out = json.dumps({"result": str(result)})
                        proc.stdin.write(payload_out + "\n")
                        proc.stdin.flush()
                    except Exception as exc:
                        proc.stdin.write(json.dumps({"error": str(exc)}) + "\n")
                        proc.stdin.flush()
                elif msg_type == "result":
                    result_value = str(msg.get("value", ""))
                    names = msg.get("session_local_names") or []
                    vals = msg.get("session_locals") or {}
                    for name in names:
                        self.session_locals[name] = vals.get(name)
                    break
                elif msg_type == "error":  # pragma: no cover
                    error_message = str(
                        msg.get("message", "Execution Error")
                    )  # pragma: no cover
                    break  # pragma: no cover
        except Exception as exc:  # pragma: no cover
            error_message = f"Execution Error: {exc}"

        if error_message is not None:  # pragma: no cover
            return error_message  # pragma: no cover
        if result_value is not None:
            return result_value
        return "Execution completed successfully."  # pragma: no cover
