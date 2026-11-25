"""OKF Filesystem Provider service.

Scans roots one level deep for directories containing index.md with
okf_version frontmatter. Registers discovered bundles with the OKF provider
registry.
"""

from __future__ import annotations

from pathlib import Path

from animas import Context, Service

from mth_components.bridges.okf_registry.service import (
    OKFBundleInfo,
)

_OKF_SCOPE_RANK = {"project": 0, "home": 1, "user": 2, "custom": 3}


def _parse_okf_frontmatter(text: str) -> dict[str, str]:
    """Parse YAML frontmatter from an OKF index.md file.

    Extracts okf_version, title, and description. Zero-dependency parser
    for the simple frontmatter used in OKF bundle root index.md files.
    """
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}
    raw = text[4:end]
    data: dict[str, str] = {}
    for raw_line in raw.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, val = stripped.split(":", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key in ("okf_version", "title", "description"):
            data[key] = val
    return data


class OKFFilesystemProviderService(Service):
    """Local filesystem provider for OKF knowledge bundles.

    Scans roots one level deep for <name>/index.md with okf_version
    frontmatter. Registers discovered bundles with the OKF provider registry.
    """

    def __init__(self, ctx: Context):
        super().__init__(ctx, "okf_filesystem")
        self._discovered: list[str] = []
        self.ctx.effect(self._cleanup)

    def discover_bundles(
        self, roots: list[tuple[Path, str]] | None = None
    ) -> list[str]:
        """Scan roots one level deep for OKF bundle roots.

        Each root carries a scope; lower rank wins when two roots expose the
        same bundle name. Unknown scopes fail closed to the lowest priority.
        Returns the sorted list of newly registered bundle names.
        """
        if roots is None:
            roots = [(Path("agents-home/knowledge"), "project")]
        if not roots:
            return []

        best: dict[str, OKFBundleInfo] = {}
        seen_paths: set[Path] = set()
        for root, scope in roots:
            if not root.is_dir():
                continue
            rank = _OKF_SCOPE_RANK.get(scope, 3)
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                resolved = child.resolve()
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)
                index_md = child / "index.md"
                if not index_md.is_file():
                    continue
                try:
                    text = index_md.read_text(encoding="utf-8")
                except OSError:
                    continue
                frontmatter = _parse_okf_frontmatter(text)
                okf_version = frontmatter.get("okf_version")
                if not okf_version:
                    continue
                name = child.name
                info = OKFBundleInfo(
                    name=name,
                    title=frontmatter.get("title", name),
                    description=frontmatter.get("description", ""),
                    path=child,
                    okf_version=okf_version,
                    rank=rank,
                )
                existing = best.get(name)
                if existing is None or info.rank < existing.rank:
                    best[name] = info

        registry = self.ctx.okf_registry
        discovered = [
            name for name in sorted(best) if registry.register_bundle(best[name])
        ]
        self._discovered = discovered
        return discovered

    def _cleanup(self) -> None:
        self._discovered.clear()
