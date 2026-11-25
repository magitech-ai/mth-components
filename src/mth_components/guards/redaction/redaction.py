"""Redact known secret values from tool output.

A model can ask a tool to read a local ``.env`` or print an environment
variable. Redaction at the tool-result boundary prevents that value from
reaching the conversation, regardless of which tool produced it.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

SECRET_ENV_NAMES: tuple[str, ...] = (
    "NVIDIA_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
)

REDACTION_MARKER = "<redacted>"


def secret_values(names: Sequence[str] = SECRET_ENV_NAMES) -> tuple[str, ...]:
    """Return currently set secret values for *names* from the environment."""
    return tuple(value for name in names if (value := os.environ.get(name)))


def redact(text: str, names: Sequence[str] = SECRET_ENV_NAMES) -> str:
    """Replace every known secret value in *text* with the redaction marker."""
    for value in secret_values(names):
        text = text.replace(value, REDACTION_MARKER)
    return text
