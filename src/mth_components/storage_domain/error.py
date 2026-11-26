"""Error vocabulary of the domain data form."""

from __future__ import annotations

from typing import Any

DOMAIN_ERROR_CODES = frozenset(
    {"already-open", "facet-unsupported", "invalid-record", "missing-key", "closed"}
)


class DomainError(Exception):
    """Stable failure taxonomy for the domain layer."""

    name = "DomainError"

    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: dict[str, str] | None = None,
        cause: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.cause = cause


__all__ = ["DOMAIN_ERROR_CODES", "DomainError"]
