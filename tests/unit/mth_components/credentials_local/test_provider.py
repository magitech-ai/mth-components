"""Unit tests for the core local credentials provider."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from animas import Context
from magitech_harness.credentials import CredentialService

import mth_components.credentials_local.provider as provider_module
from mth_components.credentials_local.component import apply
from mth_components.credentials_local.document import (
    DOCUMENT_VERSION,
    _assert_json,
    parse_document,
    render_document,
)
from mth_components.credentials_local.provider import (
    LocalCredentialProvider,
    _parse_env_file,
    assert_owner_only,
    resolve_spec,
)


def _spec(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    return resolve_spec({"path": str(tmp_path / "creds.yaml"), **overrides})


def test_document_parsing() -> None:
    empty = parse_document("", "creds.yaml")
    assert empty == {"refs": {}, "records": {}, "ignored_keys": []}
    assert parse_document("# comment\n", "creds.yaml")["refs"] == {}

    text = "\n".join(
        [
            "version: 1",
            "refs:",
            "  API_KEY: value",
            "records:",
            "  llm-demo/main:",
            "    kind: api-key",
            "    key: token",
            "    env:",
            "      OTHER_KEY: other",
            "  grant-demo/g:",
            "    kind: grant",
            "    payload:",
            "      count: 2",
            "foreign: kept",
        ]
    )
    parsed = parse_document(text, "creds.yaml")
    assert parsed["refs"] == {"API_KEY": "value"}
    assert parsed["records"]["llm-demo/main"]["env"] == {"OTHER_KEY": "other"}
    assert parsed["records"]["grant-demo/g"]["payload"] == {"count": 2}
    assert parsed["ignored_keys"] == {"foreign": "kept"}
    rendered = render_document(parsed["refs"], parsed["records"])
    assert parse_document(rendered, "creds.yaml")["refs"] == {"API_KEY": "value"}
    assert DOCUMENT_VERSION == 1


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        "KEY: value",
        "version: 2\nrefs: {}",
        "version: 1\nrefs: []",
        "version: 1\nrecords: []",
        "version: 1\nrefs:\n  bad-key: value",
        "version: 1\nrefs:\n  KEY: 12",
        "version: 1\nrefs:\n  KEY: ''",
        "version: 1\nrecords:\n  bad: {}",
        "version: 1\nrecords:\n  llm-demo/a: 3",
        "version: 1\nrecords:\n  llm-demo/a:\n    kind: weird",
        "version: 1\nrecords:\n  llm-demo/a:\n    payload: {}",
        "version: 1\nrecords:\n  llm-demo/a:\n    kind: api-key\n    extra: 1",
        "version: 1\nrecords:\n  llm-demo/a:\n    kind: api-key\n    key: ''",
        "version: 1\nrecords:\n  llm-demo/a:\n    kind: api-key\n    env: []",
        "version: 1\nrecords:\n  llm-demo/a:\n    kind: api-key\n    env:\n"
        "      BAD-KEY: x",
        "version: 1\nrecords:\n  llm-demo/a:\n    kind: api-key\n    env:\n"
        "      KEY: ''",
        "version: 1\nrecords:\n  llm-demo/a:\n    kind: grant\n    extra: 1",
        "version: 1\nrecords:\n  llm-demo/a:\n    kind: grant\n    payload: .inf",
        "- broken",
    ],
)
def test_document_rejections(text: str) -> None:
    with pytest.raises((ValueError, TypeError)):
        parse_document(text, "creds.yaml")


def test_owner_only_and_spec(tmp_path: Path) -> None:
    spec = _spec(tmp_path, watch=False, debounce_ms=25)
    assert spec["filename"] == tmp_path / "creds.yaml"
    assert spec["watch"] is False
    assert spec["debounce_ms"] == 25
    assert resolve_spec({})["filename"].name == ".credentials.yaml"

    loose = tmp_path / "loose.yaml"
    loose.write_text("version: 1\n", encoding="utf-8")
    loose.chmod(0o644)
    with pytest.raises(PermissionError, match="chmod 600"):
        assert_owner_only(loose)
    assert_owner_only(tmp_path / "missing.yaml")


def test_provider_resolution_and_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = tmp_path / "creds.yaml"
    document.write_text("version: 1\nrefs:\n  STORED: saved\n", encoding="utf-8")
    document.chmod(0o600)
    monkeypatch.setenv("PROCESS_KEY", "process")
    (tmp_path / ".env").write_text("CWD_KEY=cwd\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    provider = LocalCredentialProvider(_spec(tmp_path, dsh_home=str(tmp_path / "home")))
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / ".env").write_text("HOME_KEY=home\n", encoding="utf-8")
    provider.load()

    assert provider.resolve("STORED") == {"value": "saved", "source": "stored"}  # type: ignore[arg-type]
    assert provider.resolve("PROCESS_KEY") == {  # type: ignore[arg-type]
        "value": "process",
        "source": "environment",
    }
    assert provider.resolve("CWD_KEY")["value"] == "cwd"  # type: ignore[index]
    assert provider.resolve("HOME_KEY")["value"] == "home"  # type: ignore[index]
    assert provider.resolve("ABSENT") is None  # type: ignore[arg-type]

    provider.set_ref("NEW_KEY", "new")  # type: ignore[arg-type]
    assert "NEW_KEY:" in document.read_text(encoding="utf-8")
    assert oct(document.stat().st_mode & 0o777) == "0o600"
    provider.unset_ref("NEW_KEY")  # type: ignore[arg-type]
    assert "NEW_KEY" not in document.read_text(encoding="utf-8")
    provider.set_ref("EMPTY", "")  # type: ignore[arg-type]
    assert "EMPTY" not in document.read_text(encoding="utf-8")

    provider.save_record("llm-demo/main", {"kind": "api-key", "key": "k"})
    assert "llm-demo/main" in document.read_text(encoding="utf-8")
    provider.save_record("llm-demo/main", None)
    assert "llm-demo/main" not in document.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_component_wiring(tmp_path: Path) -> None:
    ctx = Context()
    CredentialService(ctx)
    await ctx.flush()
    document = tmp_path / "creds.yaml"
    document.write_text("version: 1\nrefs:\n  STORED: saved\n", encoding="utf-8")
    document.chmod(0o600)
    await apply(ctx, path=str(document), dsh_home=str(tmp_path / "home"))
    credentials = ctx.credentials
    assert credentials.resolve(credentials.credential_ref("STORED")) == {
        "value": "saved",
        "source": "stored",
    }
    credentials.set(credentials.credential_ref("NEW"), "written")
    assert "NEW: written" in document.read_text(encoding="utf-8")
    credentials.unset(credentials.credential_ref("NEW"))
    assert "NEW:" not in document.read_text(encoding="utf-8")

    key = credentials.credential_key("llm-demo", "main")
    credentials.modify_record(key, lambda _current: {"kind": "grant", "payload": {}})
    assert "llm-demo/main" in document.read_text(encoding="utf-8")
    credentials.delete_record(key)
    assert "llm-demo/main" not in document.read_text(encoding="utf-8")

    await ctx.fiber.dispose()
    # The provider is released on disposal; the service falls back to the
    # snapshot it was hydrated with at mount time.
    assert credentials.resolve(credentials.credential_ref("STORED")) == {
        "value": "saved",
        "source": "stored",
    }
    with pytest.raises(RuntimeError, match="not mounted"):
        await apply(Context(), path=str(document))


def test_document_json_boundary_and_minimal_records() -> None:
    _assert_json("where", None)
    _assert_json("where", "text")
    _assert_json("where", True)
    _assert_json("where", 3)
    _assert_json("where", 1.5)
    _assert_json("where", {"a": [1, "b"]})
    with pytest.raises(TypeError, match="non-finite"):
        _assert_json("where", float("inf"))
    with pytest.raises(TypeError, match="cyclic"):
        cyclic: dict[str, Any] = {}
        cyclic["self"] = cyclic
        _assert_json("where", cyclic)
    with pytest.raises(TypeError, match="cannot represent"):
        _assert_json("where", object())

    text = "\n".join(
        [
            "version: 1",
            "records:",
            "  llm-demo/a:",
            "    kind: api-key",
            "  llm-demo/b:",
            "    kind: grant",
            "    payload: {}",
            "  llm-demo/c:",
            "    kind: api-key",
            "    env:",
            '      OK: "yes"',
        ]
    )
    parsed = parse_document(text, "creds.yaml")
    assert parsed["records"]["llm-demo/a"] == {"kind": "api-key"}
    assert "env" in parsed["records"]["llm-demo/c"]
    assert parse_document("{}", "creds.yaml")["refs"] == {}
    with pytest.raises(ValueError, match="invalid document"):
        parse_document("key: [unclosed", "creds.yaml")
    with pytest.raises(ValueError, match="no payload"):
        parse_document(
            "version: 1\nrecords:\n  llm-demo/a:\n    kind: grant\n", "creds.yaml"
        )


def test_provider_env_file_edges_and_write_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / "dotted.env"
    env.write_text(
        "# comment\n\nNO_EQUALS\n=empty-name\n KEY = 'quoted'\n", encoding="utf-8"
    )
    assert _parse_env_file(env) == {"KEY": "quoted"}
    assert _parse_env_file(tmp_path / "missing.env") == {}

    monkeypatch.setattr(provider_module.sys, "platform", "win32")
    loose = tmp_path / "loose.yaml"
    loose.write_text("version: 1\n", encoding="utf-8")
    loose.chmod(0o644)
    provider_module.assert_owner_only(loose)

    absent = provider_module.LocalCredentialProvider(
        provider_module.resolve_spec({"path": str(tmp_path / "absent.yaml")})
    )
    assert absent.resolve("ANY") is None  # type: ignore[arg-type]

    document = tmp_path / "creds.yaml"
    document.write_text("version: 1\nrefs: {}\nrecords: {}\n", encoding="utf-8")
    document.chmod(0o600)
    provider = provider_module.LocalCredentialProvider(
        provider_module.resolve_spec({"path": str(document)})
    )

    def fail_open(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(provider_module.os, "open", fail_open)
    with pytest.raises(OSError, match="disk full"):
        provider.set_ref("KEY", "value")  # type: ignore[arg-type]
    monkeypatch.undo()
    provider.set_ref("KEY", "value")  # type: ignore[arg-type]
    assert "KEY: value" in document.read_text(encoding="utf-8")
    monkeypatch.setattr(provider_module.sys, "platform", "win32")
    provider.set_ref("WIN", "value")  # type: ignore[arg-type]
    assert "WIN: value" in document.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_foreign_keys_duplicates_write_validation_and_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = tmp_path / "creds.yaml"
    document.write_text(
        "version: 1\nforeign: kept\nrefs:\n  OLD: saved\nrecords:\n"
        "  llm-demo/main:\n    kind: grant\n    payload: {}\n",
        encoding="utf-8",
    )
    document.chmod(0o600)
    provider = provider_module.LocalCredentialProvider(
        provider_module.resolve_spec({"path": str(document)})
    )
    assert provider.ignored == {"foreign": "kept"}
    provider.set_ref("NEW", "value")  # type: ignore[arg-type]
    rendered = document.read_text(encoding="utf-8")
    assert "foreign: kept" in rendered
    assert "NEW: value" in rendered

    with pytest.raises(ValueError, match="duplicate key"):
        parse_document("version: 1\nrefs:\n  KEY: one\n  KEY: two\n", "creds.yaml")
    with pytest.raises((ValueError, TypeError)):
        parse_document(
            "version: 1\nrecords:\n  llm_demo/main:\n    kind: grant\n"
            "    payload: {}\n",
            "creds.yaml",
        )
    with pytest.raises((ValueError, TypeError)):
        provider.save_record(
            "llm-demo/x", {"kind": "grant", "payload": {"x": float("inf")}}
        )

    ctx = Context()
    CredentialService(ctx)
    await ctx.flush()
    await apply(ctx, path=str(document))
    credentials = ctx.credentials
    credentials.unset(credentials.credential_ref("OLD"))
    assert "OLD:" not in document.read_text(encoding="utf-8")
    key = credentials.credential_key("llm-demo", "main")
    assert credentials.list_records()
    assert credentials.read_record(key) is not None
    credentials.delete_record(key)
    assert "llm-demo/main" not in document.read_text(encoding="utf-8")

    monkeypatch.setenv("MAGITECH_HOME", str(tmp_path / "magitech"))
    assert provider_module.resolve_spec({})["home"] == tmp_path / "magitech"

    def fail_replace(self: Path, target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        provider.set_ref("FAIL", "value")  # type: ignore[arg-type]
    assert not list(tmp_path.glob(".*.tmp"))


def test_write_side_key_and_ref_validation(tmp_path: Path) -> None:
    document = tmp_path / "creds.yaml"
    document.write_text("version: 1\nrefs: {}\nrecords: {}\n", encoding="utf-8")
    document.chmod(0o600)
    provider = provider_module.LocalCredentialProvider(
        provider_module.resolve_spec({"path": str(document)})
    )
    with pytest.raises(ValueError, match="invalid credential reference"):
        provider.set_ref("bad-key", "value")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid credential reference"):
        provider.unset_ref("bad-key")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid credential key"):
        provider.save_record("llm_demo/main", {"kind": "grant", "payload": {}})
    assert "bad-key" not in document.read_text(encoding="utf-8")
    assert "llm_demo" not in document.read_text(encoding="utf-8")
