"""Python source analysis tool component package."""

from mth_plugins.tools.code_analysis.component import apply
from mth_plugins.tools.code_analysis.handlers import (
    handle_code_metrics,
    handle_find_duplicates,
    handle_generate_docs,
)

__all__ = [
    "apply",
    "handle_code_metrics",
    "handle_find_duplicates",
    "handle_generate_docs",
]
