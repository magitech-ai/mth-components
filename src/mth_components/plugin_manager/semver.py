"""Small semver 2.0 validator/comparator for plugin version policy.

Pinned DSH uses the npm ``semver`` package. Only the predicates the manager
uses are needed here: validity, greater-than, reverse comparison and the
prerelease test. Build metadata is ignored in ordering, as in semver.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


@lru_cache(maxsize=256)
def parse(version: str) -> tuple[tuple[int, int, int], tuple[Any, ...] | None] | None:
    match = SEMVER.fullmatch(version)
    if match is None:
        return None
    core = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    pre = match.group(4)
    parts: tuple[Any, ...] | None = None
    if pre is not None:
        parts = tuple(int(item) if item.isdigit() else item for item in pre.split("."))
    return core, parts


def is_valid(version: str) -> bool:
    return parse(version) is not None


def prerelease(version: str) -> tuple[Any, ...] | None:
    parsed = parse(version)
    return None if parsed is None else parsed[1]


def compare(left: str, right: str) -> int:
    """Return -1, 0 or 1 ordering two valid semver strings."""
    a, b = parse(left), parse(right)
    if a is None or b is None:
        raise ValueError(f"not a valid semver version: {left!r} or {right!r}")
    if a[0] != b[0]:
        return -1 if a[0] < b[0] else 1
    ap, bp = a[1], b[1]
    if ap is None and bp is None:
        return 0
    if ap is None:
        return 1
    if bp is None:
        return -1
    for x, y in zip(ap, bp, strict=False):
        if x == y:
            continue
        if isinstance(x, int) and isinstance(y, int):
            return -1 if x < y else 1
        if isinstance(x, int):
            return -1
        if isinstance(y, int):
            return 1
        return -1 if x < y else 1
    return (len(ap) > len(bp)) - (len(ap) < len(bp))


def gt(left: str, right: str) -> bool:
    return compare(left, right) > 0


def rcompare(left: str, right: str) -> int:
    return -compare(left, right)


def _satisfies_clause(version: str, clause: str) -> bool:
    clause = clause.strip()
    if clause in ("", "*", "x", "X"):
        return True
    if clause.startswith("^"):
        base = clause[1:].strip()
        parsed = parse(base)
        if parsed is None:
            return False
        (major, minor, patch), _pre = parsed
        lower = f"{major}.{minor}.{patch}"
        if major > 0:
            upper = f"{major + 1}.0.0"
        elif minor > 0:
            upper = f"0.{minor + 1}.0"
        else:
            upper = f"0.0.{patch + 1}"
        return compare(version, lower) >= 0 and compare(version, upper) < 0
    if clause.startswith("~"):
        base = clause[1:].strip()
        parsed = parse(base)
        if parsed is None:
            return False
        (major, minor, patch), _pre = parsed
        upper = f"{major}.{minor + 1}.0"
        return (
            compare(version, f"{major}.{minor}.{patch}") >= 0
            and compare(version, upper) < 0
        )
    match = re.match(r"^(>=|<=|>|<|=)?\s*(.+)$", clause)
    assert match is not None
    operator, target = match.group(1) or "=", match.group(2).strip()
    if not is_valid(target):
        parts = target.split(".")
        if (
            1 <= len(parts) <= 3
            and parts[0].isdigit()
            and all(part in ("*", "x", "X") or part.isdigit() for part in parts)
        ):
            digits = [part for part in parts if part.isdigit()]
            prefix = ".".join(digits)
            return version == prefix or version.startswith(f"{prefix}.")
        return False
    value = compare(version, target)
    return {
        ">=": value >= 0,
        "<=": value <= 0,
        ">": value > 0,
        "<": value < 0,
        "=": value == 0,
    }[operator]


def satisfies(version: str, range_spec: str) -> bool:
    """npm-style range membership with prereleases admitted."""
    version = version[1:] if version.startswith("v") else version
    if not is_valid(version):
        return False
    for alternative in range_spec.split("||"):
        clauses = alternative.strip().split()
        if clauses and all(_satisfies_clause(version, clause) for clause in clauses):
            return True
    return False


__all__ = [
    "compare",
    "gt",
    "is_valid",
    "parse",
    "prerelease",
    "rcompare",
    "satisfies",
]
