"""Long-running subprocess entry point for the CodeAct PTC runtime."""

from __future__ import annotations

import asyncio
import importlib.util as _ilu
import json
import sys
from pathlib import Path
from typing import Any

_transform_path = Path(__file__).resolve().parent / "transform.py"
_spec = _ilu.spec_from_file_location("codeact_transform", _transform_path)
assert _spec is not None and _spec.loader is not None
_transform = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_transform)
SAFE_BUILTINS = _transform.SAFE_BUILTINS
FinalAnswerError = _transform.FinalAnswerError
transform_code = _transform.transform_code


def _dispatch_via_ipc(tool_name: str, *args: Any, **kwargs: Any) -> Any:
    request = {
        "type": "tool_call",
        "name": tool_name,
        "args": list(args),
        "kwargs": kwargs,
    }
    sys.stdout.write(json.dumps(request) + "\n")
    sys.stdout.flush()
    response_line = sys.stdin.readline()
    if not response_line:
        raise RuntimeError(f"parent closed while invoking tool '{tool_name}'")
    response = json.loads(response_line)
    if "error" in response:
        raise RuntimeError(response["error"])
    return response.get("result")


def _exec_one(
    code: str, tool_names: set[str], session_locals: dict[str, Any]
) -> tuple[str, list[str], dict[str, Any]]:
    try:
        mod, top_names = transform_code(code, tool_names)
        compiled = compile(mod, filename="<codeact>", mode="exec")
    except (ValueError, SyntaxError) as exc:
        return f"CodeAct Error: {exc}", [], {}

    async def _dispatch_wrapper(tool_name: str, *t_args: Any, **t_kwargs: Any) -> Any:
        return _dispatch_via_ipc(tool_name, *t_args, **t_kwargs)

    def _final_answer_wrapper(val: Any) -> None:
        raise FinalAnswerError(val)

    exec_globals = dict(session_locals)
    exec_globals["__name__"] = "__main__"
    exec_globals["__builtins__"] = dict(SAFE_BUILTINS)

    persisted_names: list[str] = []
    serializable: dict[str, Any] = {}
    try:
        exec(compiled, exec_globals)
        runner = exec_globals["__codeact_runner__"]

        async def _main() -> Any:
            return await runner(_dispatch_wrapper, _final_answer_wrapper)

        last_val = asyncio.run(_main())
    except FinalAnswerError as fa:
        for k in top_names:
            if k in exec_globals and not k.startswith("__"):
                session_locals[k] = exec_globals[k]
                persisted_names.append(k)
                try:
                    json.dumps(exec_globals[k])
                    serializable[k] = exec_globals[k]
                except (TypeError, ValueError):
                    serializable[k] = None
        return f"Final Answer: {fa.value}", persisted_names, serializable
    except Exception as exc:
        return f"Execution Error: {exc}", [], {}

    for k in top_names:
        if k in exec_globals and not k.startswith("__"):
            session_locals[k] = exec_globals[k]
            persisted_names.append(k)
            try:
                json.dumps(exec_globals[k])
                serializable[k] = exec_globals[k]
            except (TypeError, ValueError):
                serializable[k] = None

    if last_val is not None:
        return str(last_val), persisted_names, serializable
    return "Execution completed successfully.", persisted_names, serializable


def main() -> None:
    session_locals: dict[str, Any] = {}
    while True:
        raw = sys.stdin.readline()
        if not raw:
            break
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"type": "error", "message": "bad input"}
            sys.stdout.write(json.dumps(payload) + "\n")
            sys.stdout.flush()
            continue
        code = str(payload.get("code", ""))
        tool_names = set(payload.get("tool_names", []))
        result, names, serializable = _exec_one(code, tool_names, session_locals)
        # Update session_locals in subprocess memory for serializable + names
        for _name in names:
            # We don't have the actual object here (subprocess keeps them in
            # exec_globals which is discarded). Re-execute tracking is done via
            # names only; values come from serializable.
            pass
        # session_locals already holds the actual objects from _exec_one.
        sys.stdout.write(
            json.dumps(
                {
                    "type": "result",
                    "value": result,
                    "session_locals": serializable,
                    "session_local_names": names,
                }
            )
            + "\n"
        )
        sys.stdout.flush()


if __name__ == "__main__":
    main()
