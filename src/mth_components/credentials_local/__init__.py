"""Core local credentials provider component."""

from __future__ import annotations

from mth_components.credentials_local.document import (
    DOCUMENT_VERSION,
    parse_document,
    render_document,
)
from mth_components.credentials_local.provider import (
    CREDENTIALS_FILENAME,
    LocalCredentialProvider,
    assert_owner_only,
    resolve_spec,
)

__all__ = [
    "CREDENTIALS_FILENAME",
    "DOCUMENT_VERSION",
    "LocalCredentialProvider",
    "assert_owner_only",
    "parse_document",
    "render_document",
    "resolve_spec",
]
