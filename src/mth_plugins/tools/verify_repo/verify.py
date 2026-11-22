"""Per-repository verification helpers (portable from home-swe-home)."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Runner = Callable[[str, Path], tuple[int, str]]

DEFAULT_CHECKS = (
    "uv sync --extra dev",
    "uv run ruff check src tests",
    "uv run mypy src",
    "uv run pytest",
)


@dataclass(frozen=True)
class CheckResult:
    command: str
    ok: bool
    output: str


def verification_commands(repo_root: Path) -> tuple[str, ...]:
    path = repo_root / "docs" / "agents" / "checks.md"
    if not path.is_file():
        return DEFAULT_CHECKS
    commands: list[str] = []
    in_block = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("```sh") or stripped.startswith("```bash"):
            in_block = True
            continue
        if in_block and stripped.startswith("```"):
            break
        if in_block and stripped and not stripped.startswith("#"):
            commands.append(stripped)
    return tuple(commands) or DEFAULT_CHECKS


def default_runner(command: str, cwd: Path) -> tuple[int, str]:
    completed = subprocess.run(
        shlex.split(command),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def format_results(results: list[CheckResult]) -> str:
    if not results:
        return "No checks to run."
    lines: list[str] = []
    for result in results:
        lines.append(f"[{'PASS' if result.ok else 'FAIL'}] {result.command}")
        if not result.ok and result.output:
            lines.append(result.output[-800:])
    passed = sum(1 for r in results if r.ok)
    lines.append(f"{passed}/{len(results)} checks passed")
    return "\n".join(lines)


def handle_verify_repo(args: dict[str, Any], runner: Runner = default_runner) -> str:
    root = Path(args.get("path", ".")).resolve()
    commands = verification_commands(root)
    results: list[CheckResult] = []
    for command in commands:
        code, output = runner(command, root)
        results.append(CheckResult(command, code == 0, output))
    return format_results(results)
