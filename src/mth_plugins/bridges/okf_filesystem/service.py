"""OKF Filesystem Provider service.

Scans roots one level deep for directories containing index.md with
okf_version frontmatter. Registers discovered bundles with the OKF provider
registry.
"""

from __future__ import annotations

from pathlib import Path

from anima import Context, Service

from mth_plugins.bridges.okf_registry.service import (
    OKFBundleInfo,
)


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

    def discover_bundles(self, root: Path | None = None) -> list[str]:
        """Scan root one level deep for OKF bundle roots.

        A bundle root is a directory containing index.md with okf_version
        frontmatter. Returns list of bundle names discovered.
        """
        search_root = root or Path("agents-home/knowledge")
        if not search_root.is_dir():
            return []

        discovered: list[str] = []
        for child in sorted(search_root.iterdir()):
            if not child.is_dir():
                continue
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
            title = frontmatter.get("title", name)
            description = frontmatter.get("description", "")
            bundle_info = OKFBundleInfo(
                name=name,
                title=title,
                description=description,
                path=child,
                okf_version=okf_version,
                rank=0,
            )
            registry = self.ctx.okf_registry
            if registry.register_bundle(bundle_info):
                discovered.append(name)
        self._discovered = discovered
        return discovered

    def _cleanup(self) -> None:
        self._discovered.clear()
