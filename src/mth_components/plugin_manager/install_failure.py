"""Classify package-operation failures by their exact diagnostic signature.

Ports ``packages/boot/plugin-manager/src/install-failure.ts``: precedence is
timeouts, then a missing executable, then the ordered log families, then
``unknown``.
"""

from __future__ import annotations

import re
from typing import Any

LOG_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "build-blocked",
        re.compile(
            "ERR_PNPM_IGNORED_BUILDS|Ignored build scripts|"
            "ERR_PNPM_GIT_DEP_PREPARE_NOT_ALLOWED"
        ),
    ),
    (
        "not-found",
        re.compile(r"ERR_PNPM_FETCH_404|\bE404\b|404 Not Found|Not Found - GET/"),
    ),
    (
        "no-matching-version",
        re.compile(r"ERR_PNPM_NO_MATCHING_VERSION|\bETARGET\b|No matching version"),
    ),
    ("disk-full", re.compile(r"\bENOSPC\b|no space left on device", re.IGNORECASE)),
    (
        "permission",
        re.compile(r"\bEACCES\b|\bEPERM\b|permission denied", re.IGNORECASE),
    ),
    (
        "integrity",
        re.compile(
            r"ERR_PNPM_TARBALL_INTEGRITY|ERR_PNPM_BAD_TARBALL_SIZE|\bEINTEGRITY\b"
        ),
    ),
    ("unknown", re.compile(r"The requested URL returned error: 40[134]\b")),
    (
        "network",
        re.compile(
            r"\bENOTFOUND\b|\bECONNRESET\b|\bETIMEDOUT\b|\bECONNREFUSED\b|"
            r"\bEAI_AGAIN\b|ERR_PNPM_META_FETCH_FAIL|ERR_PNPM_FETCH_5\d\d|"
            r"ERR_PNPM_FETCH_TIMEOUT|\bFETCH_ERROR\b|socket hang up|"
            r"Could not resolve host|unable to access",
            re.IGNORECASE,
        ),
    ),
)


def classify_install_failure(
    log: str, cause: Any = None, timed_out: bool = False
) -> str:
    """Return the stable failure kind for one package operation."""
    if timed_out:
        return "timeout"
    code = getattr(cause, "errno", None) or getattr(cause, "code", None)
    if code in (2, "ENOENT") or isinstance(cause, FileNotFoundError):
        return "pnpm-missing"
    for kind, pattern in LOG_KINDS:
        if pattern.search(log):
            return kind
    return "unknown"


__all__ = ["LOG_KINDS", "classify_install_failure"]
