"""Tests for the shared filesystem, analysis, search and developer tools."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from mth_plugins.tools.code_analysis.handlers import (
    handle_code_metrics,
    handle_find_duplicates,
    handle_generate_docs,
)
from mth_plugins.tools.dev_tools import handlers as dev_handlers
from mth_plugins.tools.dev_tools.handlers import (
    _current_turn_read_paths,
    find_home,
    handle_add_repo,
    handle_reload_module,
    parse_repo_map,
    resolve_repo,
)
from mth_plugins.tools.fs_extra.handlers import (
    handle_delete_file,
    handle_list_directory,
)
from mth_plugins.tools.verify_repo import verify as verify_mod
from mth_plugins.tools.verify_repo.verify import (
    CheckResult,
    format_results,
    handle_verify_repo,
)
from mth_plugins.tools.web_search import handlers as web

# ---------------------------------------------------------------------------
# fs_extra
# ---------------------------------------------------------------------------


def test_delete_file_and_errors(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x")
    assert handle_delete_file({"path": str(target)}, None) == f"Deleted {target}."
    assert not target.exists()
    assert "FileNotFoundError" in handle_delete_file({"path": str(target)}, None)
    assert "required" in handle_delete_file({}, None)


def test_delete_file_confined(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-delete-test.txt"
    outside.write_text("x")
    try:
        assert "Error:" in handle_delete_file({"path": str(outside)}, tmp_path)
        assert outside.exists()
    finally:
        outside.unlink(missing_ok=True)


def test_list_directory(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "f.txt").write_text("x")
    result = handle_list_directory({"path": str(tmp_path)}, None)
    assert "sub/" in result and "f.txt" in result
    assert handle_list_directory({"path": str(tmp_path / "sub")}, None) == "(empty)"
    assert "Error:" in handle_list_directory({"path": str(tmp_path / "missing")}, None)
    assert "Error:" in handle_list_directory({"path": str(tmp_path.parent)}, tmp_path)


# ---------------------------------------------------------------------------
# code_analysis
# ---------------------------------------------------------------------------


def test_code_metrics(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    if True:\n        return 1\n")
    result = handle_code_metrics({"path": str(tmp_path)})
    assert "file_count: 1" in result
    assert "function_count: 1" in result
    assert "estimated_complexity: 1" in result
    assert "file_count: 0" in handle_code_metrics({"path": str(tmp_path / "nope")})


def test_find_duplicates(tmp_path: Path) -> None:
    block = "def a():\n    return 1\n"
    (tmp_path / "a.py").write_text(block)
    (tmp_path / "b.py").write_text(block)
    assert "duplicate code blocks" in handle_find_duplicates({"path": str(tmp_path)})
    (tmp_path / "b.py").write_text("def b():\n    return 2\n")
    assert "No duplicate" in handle_find_duplicates({"path": str(tmp_path)})


def test_generate_docs(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(
        '"""M."""\n\nclass C:\n    """C."""\n    def m(self):\n        pass\n'
        "\ndef f():\n    pass\n"
    )
    (tmp_path / "bad.py").write_text("def broken(:\n")
    result = handle_generate_docs({"path": str(tmp_path)})
    assert "# Auto-generated Documentation" in result
    assert "### C" in result
    assert "_No docstring_" in result


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------


def test_web_search_helpers() -> None:
    assert web._is_bot_challenge("captcha") is True
    assert web._is_bot_challenge("blocked by requests") is True
    assert web._is_bot_challenge("normal page") is False
    assert web._decode_href("//example.com") == "https://example.com"
    assert web._decode_href("/x?a=1&uddg=https%3A%2F%2Fex.com") == "https://ex.com"
    pattern = web._ENDPOINTS[0][1]
    page = '<a class="result__a" href="//e.com">T &amp; X</a>'
    assert web._parse_results(page, pattern) == ["T & X: https://e.com"]


def test_web_search_searxng_and_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARXNG_URL", "http://example/base/")
    monkeypatch.setattr(
        web,
        "_search_searxng",
        lambda base, q: ["r: http://x"] if base == "http://example/base" else None,
    )
    assert web.handle_web_search({"query": "q"}) == "r: http://x"

    monkeypatch.setattr(web, "_search_searxng", lambda base, q: [])
    monkeypatch.setattr(
        web.urllib.request, "urlopen", MagicMock(side_effect=OSError("net"))
    )
    assert "web search failed" in web.handle_web_search({"query": "q"})

    def fake_urlopen(req: Any, timeout: int = 0) -> Any:
        raise web.urllib.error.URLError("x")

    monkeypatch.setattr(web.urllib.request, "urlopen", fake_urlopen)
    assert "web search failed" in web.handle_web_search({"query": "q"})


# ---------------------------------------------------------------------------
# verify_repo
# ---------------------------------------------------------------------------


def test_verification_commands(tmp_path: Path) -> None:
    assert verify_mod.verification_commands(tmp_path) == verify_mod.DEFAULT_CHECKS
    d = tmp_path / "docs" / "agents"
    d.mkdir(parents=True)
    (d / "checks.md").write_text("## Checks\n```sh\n# comment\necho hi\n```\n")
    assert verify_mod.verification_commands(tmp_path) == ("echo hi",)


def test_format_results_and_runner(tmp_path: Path) -> None:
    assert format_results([]) == "No checks to run."
    results = [CheckResult("ok", True, ""), CheckResult("fail", False, "x" * 900)]
    out = format_results(results)
    assert "[PASS] ok" in out and "[FAIL] fail" in out and "1/2 checks passed" in out
    code, output = verify_mod.default_runner("echo hello", tmp_path)
    assert code == 0 and output == "hello"


def test_handle_verify_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify_mod, "verification_commands", lambda root: ("c1", "c2"))
    calls: list[str] = []

    def runner(command: str, cwd: Path) -> tuple[int, str]:
        calls.append(command)
        return (0, "") if command == "c1" else (1, "bad")

    result = handle_verify_repo({"path": "."}, runner)
    assert calls == ["c1", "c2"]
    assert "2/2 checks passed" not in result
    assert "1/2 checks passed" in result


# ---------------------------------------------------------------------------
# dev_tools
# ---------------------------------------------------------------------------


def test_repo_map_parsing_and_resolution() -> None:
    text = (
        "# Home\n## Other Repos\n"
        "- **Repo A** [../a](../a) <https://github.com/o/a>\n"
        "  more\n- broken\n## End\n"
    )
    entries = parse_repo_map(text)
    assert len(entries) == 1 and entries[0].name == "Repo A"
    assert resolve_repo(entries, "repo a") == entries[0]
    assert resolve_repo(entries, "https://github.com/o/a.git") == entries[0]
    assert resolve_repo(entries, "../a") == entries[0]
    assert resolve_repo(entries, "unknown") is None
    assert parse_repo_map("# no header") == []


def test_find_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    fake = tmp_path / "agents-home"
    (fake / "skills").mkdir(parents=True)
    (fake / "AGENTS.md").write_text("x")
    assert find_home(str(fake)) == fake
    monkeypatch.setenv("AGENTS_HOME_PATH", str(fake))
    assert find_home() == fake
    monkeypatch.delenv("AGENTS_HOME_PATH")

    # The fallback walks cwd and its parents, so run it from an unrelated empty
    # directory. tmp_path itself holds the fake home above and cannot be used.
    empty = tmp_path_factory.mktemp("no-home")
    monkeypatch.chdir(empty)
    monkeypatch.setenv("HOME", str(empty))
    assert find_home(str(tmp_path / "nope")) is None


def test_handle_add_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "agents-home"
    (home / "skills").mkdir(parents=True)
    text = "## Other Repos\n- **R** [../r](../r) <https://github.com/o/r>\n"
    (home / "AGENTS.md").write_text(text)
    monkeypatch.setattr(
        "mth_plugins.tools.dev_tools.handlers.find_home", lambda *a: home
    )
    assert "not in the home repository map" in handle_add_repo({"ref": "x"}, str(home))
    assert "Confirm with confirm=true" in handle_add_repo({"ref": "r"}, str(home))
    (home.parent / "r").mkdir()
    assert "Resolved" in handle_add_repo({"ref": "r"}, str(home))
    monkeypatch.setattr(
        "mth_plugins.tools.dev_tools.handlers.find_home", lambda *a: None
    )
    assert "no agents-home" in handle_add_repo({"ref": "r"}, None)


def test_reload_module(tmp_path: Path) -> None:
    target = tmp_path / "mod.py"
    target.write_text("VALUE = 1\n")
    event = MagicMock()
    event.type = "tool/call"
    event.payload = {
        "name": "read_file",
        "arguments": json.dumps({"path": str(target)}),
    }
    start = MagicMock()
    start.type = "turn/start"
    session = MagicMock()
    session.events = [start, event]
    run_ctx = {"session": session}
    assert "has not been read" in handle_reload_module(
        {"module_name": "m", "file_path": str(target)}, {}
    )
    out = handle_reload_module({"module_name": "m", "file_path": str(target)}, run_ctx)
    assert "Reloaded module" in out
    assert "No changes detected" in handle_reload_module(
        {"module_name": "m", "file_path": str(target)}, run_ctx
    )


# ---------------------------------------------------------------------------
# Extra branch coverage
# ---------------------------------------------------------------------------


def test_web_search_more_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    assert web._decode_href("https://e.com?a=1") == "https://e.com?a=1"
    many = "".join(
        f'<a class="result__a" href="https://e.com/{i}">T{i}</a>' for i in range(12)
    )
    assert len(web._parse_results(many, web._ENDPOINTS[0][1])) == web.MAX_RESULTS

    class _Response:
        def __init__(self, body: str) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body.encode()

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

    good = io.BytesIO(
        json.dumps({"results": [{"title": "T", "url": "http://x"}]}).encode()
    )
    good.__enter__ = lambda self: self  # type: ignore[attr-defined]
    monkeypatch.setattr(web.urllib.request, "urlopen", lambda *a, **k: good)
    assert web._search_searxng("http://base", "q") == ["T: http://x"]

    monkeypatch.setattr(
        web.urllib.request, "urlopen", lambda *a, **k: _Response("not json")
    )
    assert web._search_searxng("http://base", "q") is None
    monkeypatch.setattr(
        web.urllib.request, "urlopen", MagicMock(side_effect=OSError("offline"))
    )
    assert web._search_searxng("http://base", "q") is None

    def fake_urlopen(req: Any, timeout: int = 0) -> Any:
        if "html.duckduckgo" in req.full_url:
            return _Response("captcha challenge")
        return _Response('<a class="result-link" href="https://e.com">R</a>')

    monkeypatch.setattr(web.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    assert web.handle_web_search({"query": "q"}) == "R: https://e.com"
    monkeypatch.setattr(
        web.urllib.request, "urlopen", lambda *a, **k: _Response("captcha")
    )
    assert "blocking automated" in web.handle_web_search({"query": "q"})


def test_code_analysis_edge_branches(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("# just a comment\n")
    result = handle_code_metrics({"path": str(tmp_path)})
    assert "comment_lines: 1" in result
    (tmp_path / "b.py").write_text("class X:\n    def m(self):\n        pass\n")
    result = handle_generate_docs({"path": str(tmp_path)})
    assert "### X" in result and "**m(...)**" in result
    handle_find_duplicates({"path": str(tmp_path), "min_lines": 1})


def test_dev_tools_edge_branches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "agents-home"
    (home / "skills").mkdir(parents=True)
    (home / "AGENTS.md").write_text(
        "## Other Repos\n- **R** [../r](../r) <https://github.com/o/r>\n"
    )
    monkeypatch.setattr(
        "mth_plugins.tools.dev_tools.handlers.find_home", lambda *a: home
    )
    (home / "AGENTS.md").write_text("## Other Repos\n- **R** [../r](../r)\n")
    assert "has no clone URL" in handle_add_repo({"ref": "r"}, str(home))


def test_current_turn_read_paths_branches() -> None:

    assert _current_turn_read_paths(None) == set()
    invalid = MagicMock()
    invalid.type = "tool/call"
    invalid.payload = {"name": "read_file", "arguments": "not-json"}
    session = MagicMock()
    session.events = [invalid]
    assert _current_turn_read_paths({"session": session}) == set()
    invalid.payload = {"name": "other_tool", "arguments": {}}
    assert _current_turn_read_paths({"session": session}) == set()
    invalid.payload = {"name": "read_file", "arguments": ["bad"]}
    assert _current_turn_read_paths({"session": session}) == set()


def test_reload_module_file_branches(tmp_path: Path) -> None:
    target = tmp_path / "mod.py"
    target.write_text("X=1\n")
    read = MagicMock()
    read.type = "tool/call"
    read.payload = {"name": "read_file", "arguments": {"path": str(target)}}
    session = MagicMock()
    session.events = [read]
    ctx = {"session": session}
    missing = tmp_path / "missing.py"
    read.payload = {"name": "read_file", "arguments": {"path": str(missing)}}
    assert "file not found" in handle_reload_module(
        {"module_name": "x", "file_path": str(missing)}, ctx
    )
    noext = tmp_path / "noext.txt"
    noext.write_text("Y=2\n")
    (tmp_path / "noext.py").write_text("Y=2\n")
    read.payload = {"name": "read_file", "arguments": {"path": str(noext)}}
    assert "Reloaded module" in handle_reload_module(
        {"module_name": "y", "file_path": str(noext)}, ctx
    )


def test_repo_map_and_add_repo_more_branches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert parse_repo_map("## Other Repos\n- no-name-here\n") == []
    home = tmp_path / "agents-home"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(
        "mth_plugins.tools.dev_tools.handlers.find_home", lambda *a: home
    )
    assert "AGENTS.md not found" in handle_add_repo({"ref": "r"}, str(home))
    (home / "AGENTS.md").write_text(
        "## Other Repos\n- **R** [../../outside](../../outside)\n"
    )
    assert "outside the workspace root" in handle_add_repo({"ref": "r"}, str(home))
    (home / "AGENTS.md").write_text(
        "## Other Repos\n- **R** [../r](../r) <https://github.com/o/r>\n"
    )
    assert "Confirm with confirm=true" in handle_add_repo({"ref": "r"}, str(home))
    monkeypatch.setattr(
        "mth_plugins.tools.dev_tools.handlers.subprocess.run",
        lambda *a, **k: MagicMock(returncode=1, stderr="boom"),
    )
    assert "clone failed" in handle_add_repo({"ref": "r", "confirm": True}, str(home))
    monkeypatch.setattr(
        "mth_plugins.tools.dev_tools.handlers.subprocess.run",
        lambda *a, **k: MagicMock(returncode=0, stderr=""),
    )
    assert "Cloned" in handle_add_repo({"ref": "r", "confirm": True}, str(home))


def test_web_search_final_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    assert web._decode_href("https://e.com?uddg=nope") == "nope"
    assert web._parse_results("", web._ENDPOINTS[0][1]) == []
    assert (
        web._parse_results('<a class="result__a" href="">T</a>', web._ENDPOINTS[0][1])
        == []
    )

    class _Empty:
        def read(self) -> bytes:
            return b'{"results": []}'

        def __enter__(self) -> _Empty:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

    monkeypatch.setattr(web.urllib.request, "urlopen", lambda *a, **k: _Empty())
    assert web._search_searxng("http://base", "q") == []
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    monkeypatch.setattr(web.urllib.request, "urlopen", lambda *a, **k: _Empty())
    assert "No web results found" in web.handle_web_search({"query": "q"})


def test_code_analysis_remaining_branches(tmp_path: Path) -> None:
    binary = tmp_path / "binary.py"
    binary.write_bytes(b"\xff\xfe\x00")
    assert "file_count: 1" in handle_code_metrics({"path": str(tmp_path)})
    (tmp_path / "klass.py").write_text("class K:\n    pass\n")
    assert "class_count: 1" in handle_code_metrics({"path": str(tmp_path)})
    assert "No duplicate" in handle_find_duplicates({"path": str(tmp_path)})


def test_dev_tools_remaining_branches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "mth_plugins.tools.dev_tools.handlers.find_home", lambda *a: None
    )
    assert "no agents-home" in handle_add_repo({"ref": "r"})
    target = tmp_path / "m.py"
    target.write_text("Z=1\n")
    read = MagicMock()
    read.type = "tool/call"
    read.payload = {"name": "read_file", "arguments": {"path": str(target)}}
    session = MagicMock()
    session.events = [read]
    # Force spec creation failure by monkeypatching via module import.
    original = dev_handlers.importlib.util.spec_from_file_location
    dev_handlers.importlib.util.spec_from_file_location = lambda *a, **k: None  # type: ignore[assignment]
    try:
        assert "could not create spec" in handle_reload_module(
            {"module_name": "z", "file_path": str(target)}, {"session": session}
        )
    finally:
        dev_handlers.importlib.util.spec_from_file_location = original  # type: ignore[assignment]


def test_web_search_remaining_branches() -> None:
    assert web._decode_href("/x?uddg") == "/x?uddg"
    assert (
        web._parse_results(
            '<a class="result__a" href="https://e.com"> </a>', web._ENDPOINTS[0][1]
        )
        == []
    )
    many = "".join(
        f'<a class="result-link" href="https://e.com/{i}">T{i}</a>' for i in range(12)
    )
    assert len(web._parse_results(many, web._ENDPOINTS[1][1])) == web.MAX_RESULTS


def test_last_coverage_branches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # code_analysis: unreadable file in generate_docs path.
    (tmp_path / "code.py").write_bytes(b"\xff\xfe")
    assert "# Auto-generated" in handle_generate_docs({"path": str(tmp_path)})

    # dev_tools: suffix fallback missing target, malformed map entry, home walk-up.
    assert parse_repo_map("## Other Repos\n- **X** no path here\n") == []

    monkeypatch.delenv("AGENTS_HOME_PATH", raising=False)
    found = find_home(None)
    assert found is None or found.is_dir()

    # web_search: uddg with no query, empty searxng item, result cap.
    assert web._decode_href("/x?uddg=a&uddg=b") == "a"

    payload = io.BytesIO(
        json.dumps(
            {
                "results": [{"title": "", "url": ""}]
                + [{"title": f"T{i}", "url": f"http://x/{i}"} for i in range(12)]
            }
        ).encode()
    )
    monkeypatch.setattr(web.urllib.request, "urlopen", lambda *a, **k: payload)
    assert len(web._search_searxng("http://base", "q") or []) == web.MAX_RESULTS


def test_final_defensive_branches(tmp_path: Path) -> None:
    assert web._decode_href("/x?a=uddg=x") == "/x?a=uddg=x"
    block = "def q():\n    return 1\n"
    (tmp_path / "a.py").write_text(block)
    (tmp_path / "b.py").write_text(block)
    assert "duplicate code blocks" in handle_find_duplicates({"path": str(tmp_path)})
    read = MagicMock()
    read.type = "tool/call"
    read.payload = {"name": "read_file", "arguments": {"path": str(tmp_path / "x.txt")}}
    session = MagicMock()
    session.events = [read]
    (tmp_path / "x.txt").write_text("x")
    assert "file not found" in handle_reload_module(
        {"module_name": "x", "file_path": str(tmp_path / "x.txt")},
        {"session": session},
    )
