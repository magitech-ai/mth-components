"""Upstream skills registry types (dsh ``@deepseek-ai/dsh-upstream-skills``).

An upstream skill set pins a directory of a vendor GitHub repository at one
commit plus its license. The entry skill is packaged locally; the pinned guide
files are downloaded and verified at registration time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

UpstreamSkillsState = Literal["installed", "downloading", "failed"]
PIN_PREFIX = "skills-"


class UpstreamSkillsError(Exception):
    """Stable failure taxonomy for upstream skill registration."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class UpstreamSkillSet:
    """Immutable pin of vendor guide files."""

    repository: str
    commit: str
    directory: str
    exclude: tuple[str, ...] = ()
    license: str = "LICENSE"
    bytes: int = 0
    sha256: str = ""

    def validate(self) -> None:
        if not self.repository or "/" not in self.repository:
            raise UpstreamSkillsError(
                "repository must be 'owner/repo'", "UPSTREAM_INVALID_PIN"
            )
        if not self.commit or len(self.commit) < 7:
            raise UpstreamSkillsError(
                "commit must be a full commit id", "UPSTREAM_INVALID_PIN"
            )
        if not self.directory or self.directory.endswith("/"):
            raise UpstreamSkillsError(
                "directory must be a relative path without a trailing slash",
                "UPSTREAM_INVALID_PIN",
            )
        if not self.license:
            raise UpstreamSkillsError(
                "license must name a repository-relative file", "UPSTREAM_INVALID_PIN"
            )
        if not self.sha256 or len(self.sha256) != 64:
            raise UpstreamSkillsError(
                "sha256 must be a 64-character hex digest", "UPSTREAM_INVALID_PIN"
            )

    @property
    def pin_name(self) -> str:
        return f"{PIN_PREFIX}{self.sha256[:16]}"


@dataclass(frozen=True)
class UpstreamSkillRegistration:
    """One registration binding a plugin name, entry skill and pin."""

    name: str
    entry: str
    set: UpstreamSkillSet
    skills_dir: str | None = None


@dataclass
class UpstreamSkillsHandle:
    """Live handle returned by ``register``."""

    name: str
    directory: str
    state: UpstreamSkillsState = "failed"
    watchers: list[Any] = field(default_factory=list)
    retry: Any = None
    dispose: Any = None

    def watch(self, listener: Any) -> Any:
        self.watchers.append(listener)

        def dispose() -> None:
            if listener in self.watchers:
                self.watchers.remove(listener)

        return dispose

    def notify(self) -> None:
        for listener in list(self.watchers):
            try:
                listener(self.state)
            except Exception:
                pass
