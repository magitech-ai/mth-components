"""Conformance: CodeAct blocks the dunder/subclass escape battery (finding #8)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from mth_components.tools.codeact.codeact import CodeActExecutor
from mth_components.tools.codeact.transform import transform_code


@pytest.mark.parametrize(
    "code",
    [
        "().__class__.__mro__[1].__subclasses__()",
        "[x for x in ().__class__.__mro__[1].__subclasses__()]",
        "getattr((), '__class__')",
        "x = {}.__class__",
        "type('X', (), {})",
        "().__class__",
        "import os; os.system('echo hi')",
        "import subprocess",
    ],
)
def test_escape_code_rejected(code: str) -> None:
    """Each escape payload is rejected at transform time with a ValueError."""
    with pytest.raises(ValueError):
        transform_code(code, set())


def test_dunder_name_rejected() -> None:
    with pytest.raises(ValueError, match="dunder name"):
        transform_code("x = __builtins__", set())


def test_normal_code_transforms() -> None:
    """Ordinary computation and class definitions still transform cleanly."""
    mod, names = transform_code(
        "class Greeter:\n    def hi(self): return 'hello'\n"
        "g = Greeter()\nresult = g.hi()",
        set(),
    )
    assert mod.body
    assert "g" in names or "result" in names


def test_infinite_loop_times_out_and_reports_failure() -> None:
    """An infinite loop is killed at the deadline and reported as a failure."""
    executor = CodeActExecutor(MagicMock(), timeout_s=0.3)
    try:
        result = asyncio.run(executor.execute("while True:\n    pass"))
        assert "timed out" in result
        assert executor._proc is None  # process killed
    finally:
        executor.dispose()


def test_executor_exception_fails_closed() -> None:
    """An unexpected read exception is reported, never as success."""
    executor = CodeActExecutor(MagicMock())

    def _raise(coro, timeout):
        coro.close()  # the mocked wait_for never awaits it
        raise RuntimeError("boom")

    with patch(
        "mth_components.tools.codeact.codeact.asyncio.wait_for",
        side_effect=_raise,
    ):
        result = asyncio.run(executor.execute("x = 1"))
    assert "Execution Error" in result
    executor.dispose()


def test_no_result_returns_error_string() -> None:
    """A read loop that completes with no result returns an error, not success."""
    executor = CodeActExecutor(MagicMock())

    async def _no_result() -> None:
        return None

    with (
        patch.object(executor, "_start_proc"),
        patch(
            "mth_components.tools.codeact.codeact.asyncio.wait_for",
            new=lambda coro, timeout: (coro.close(), _no_result())[1],
        ),
    ):
        executor._proc = MagicMock()
        executor._proc.stdin = MagicMock()
        executor._proc.stdout = MagicMock()
        result = asyncio.run(executor.execute("x = 1"))
    assert "no result" in result
