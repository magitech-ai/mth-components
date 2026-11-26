"""Versioned YAML document format for the local credential provider.

Ports the durable-boundary rules of ``packages/credentials/credentials-local``:
a strict ``version: 1`` mapping with ``refs`` and ``records`` sections, loud
rejection of anything unaddressable, and a plain rewrite on each edit (the
watcher/comment-preserving writer is a documented later phase).
"""

from __future__ import annotations

import math
import re
from typing import Any, cast

import yaml  # type: ignore[import-untyped]


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""

    def construct_mapping(self: Any, node: Any, deep: bool = False) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ValueError(f"credentials-local: duplicate key '{key}'")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


DOCUMENT_VERSION = 1
REF_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KEY_RE = re.compile(r"^[a-z][a-z0-9-]*/[a-z][a-z0-9-]*$")
ENV_NAME_RE = REF_RE


def _assert_ref(name: Any) -> str:
    if not isinstance(name, str) or not REF_RE.fullmatch(name):
        raise ValueError(f"credentials-local: invalid credential reference '{name}'")
    return name


def _assert_key(key: Any) -> str:
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise ValueError(f"credentials-local: invalid credential key '{key}'")
    return key


def _assert_json(where: str, value: Any, seen: set[int] | None = None) -> None:
    seen = seen or set()
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"credentials-local: {where} holds a non-finite number")
        return
    if isinstance(value, (dict, list)):
        if id(value) in seen:
            raise TypeError(f"credentials-local: {where} is cyclic")
        seen.add(id(value))
        items = value.values() if isinstance(value, dict) else value
        for item in items:
            _assert_json(where, item, seen)
        return
    raise TypeError(f"credentials-local: {where} holds a value JSON cannot represent")


def _parse_record(  # noqa: PLR0912
    key: str, value: Any, filename: str
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(
            f"credentials-local: record '{key}' in {filename} must be a mapping"
        )
    kind = value.get("kind")
    if kind == "api-key":
        unknown = set(value) - {"kind", "key", "env"}
        if unknown:
            raise ValueError(
                f"credentials-local: record '{key}' in {filename} has unknown field "
                f"'{sorted(unknown)[0]}'"
            )
        stored_key = value.get("key")
        if stored_key is not None and (
            not isinstance(stored_key, str) or not stored_key
        ):
            raise TypeError(
                f"credentials-local: record '{key}' in {filename} has a "
                "non-string or empty key"
            )
        env = value.get("env")
        parsed_env: dict[str, str] = {}
        if env is not None:
            if not isinstance(env, dict):
                raise TypeError(
                    f"credentials-local: record '{key}' in {filename} has a "
                    "non-mapping env"
                )
            for name, entry in env.items():
                _assert_ref(name)
                if not isinstance(entry, str) or not entry:
                    raise TypeError(
                        f"credentials-local: record '{key}' env '{name}' in {filename} "
                        "must be a non-empty string"
                    )
                parsed_env[name] = entry
        result: dict[str, Any] = {"kind": "api-key"}
        if stored_key is not None:
            result["key"] = stored_key
        if env is not None:
            result["env"] = parsed_env
        return result
    if kind == "grant":
        unknown = set(value) - {"kind", "payload"}
        if unknown:
            raise ValueError(
                f"credentials-local: record '{key}' in {filename} has unknown field "
                f"'{sorted(unknown)[0]}'"
            )
        if "payload" not in value:
            raise ValueError(
                f"credentials-local: record '{key}' in {filename} has no payload"
            )
        _assert_json(f'record "{key}" payload in {filename}', value["payload"])
        return {"kind": "grant", "payload": value["payload"]}
    if kind is None:
        raise ValueError(f"credentials-local: record '{key}' in {filename} has no kind")
    raise ValueError(
        f"credentials-local: record '{key}' in {filename} has unknown kind '{kind}'"
    )


def parse_document(  # noqa: PLR0912
    text: str, filename: str
) -> dict[str, Any]:
    """Parse and validate a credentials document."""
    try:
        root = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as error:
        raise ValueError(
            f"credentials-local: invalid document at {filename}: "
            f"{error.problem or 'yaml error'}"
        ) from error
    if root is None:
        return {"refs": {}, "records": {}, "ignored_keys": []}
    if not isinstance(root, dict):
        raise TypeError(f"credentials-local: {filename} must be a mapping")
    keys = list(root)
    if not keys:
        return {"refs": {}, "records": {}, "ignored_keys": []}
    if "version" not in root:
        raise ValueError(
            f"credentials-local: {filename} uses the pre-release flat layout. "
            f"Add 'version: {DOCUMENT_VERSION}'."
        )
    if root["version"] != DOCUMENT_VERSION:
        raise ValueError(
            f"credentials-local: {filename} declares version {root['version']}; "
            f"this build reads version {DOCUMENT_VERSION}"
        )
    refs_section = root.get("refs")
    if refs_section is None:
        refs_section = {}
    records_section = root.get("records")
    if records_section is None:
        records_section = {}
    if not isinstance(refs_section, dict):
        raise TypeError(f'credentials-local: "refs" in {filename} must be a mapping')
    if not isinstance(records_section, dict):
        raise TypeError(f'credentials-local: "records" in {filename} must be a mapping')
    refs: dict[str, str] = {}
    for name, value in refs_section.items():
        _assert_ref(name)
        if not isinstance(value, str):
            raise TypeError(
                f'credentials-local: the value for "{name}" in {filename} '
                "must be a string"
            )
        if not value:
            raise ValueError(
                f'credentials-local: the value for "{name}" in {filename} is empty; '
                "remove the key instead"
            )
        refs[name] = value
    records: dict[str, dict[str, Any]] = {}
    for key, value in records_section.items():
        _assert_key(key)
        records[key] = _parse_record(key, value, filename)
    ignored = {
        key: root[key] for key in keys if key not in ("version", "refs", "records")
    }
    return {"refs": refs, "records": records, "ignored_keys": ignored}


def render_document(
    refs: dict[str, str],
    records: dict[str, dict[str, Any]],
    ignored: dict[str, Any] | None = None,
) -> str:
    """Render the current store as version-1 YAML."""
    document: dict[str, Any] = dict(ignored or {})
    document.update(
        {
            "version": DOCUMENT_VERSION,
            "refs": dict(sorted(refs.items())),
            "records": dict(sorted(records.items())),
        }
    )
    return cast(str, yaml.safe_dump(document, sort_keys=True, allow_unicode=True))


__all__ = [
    "DOCUMENT_VERSION",
    "parse_document",
    "render_document",
]
