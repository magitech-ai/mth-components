"""OKF Provider Registry service.

Pure provider registry for OKF knowledge bundles. Owns conflict resolution
(first-wins by rank) and consumer access.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from anima import Context, Service


@dataclass(frozen=True)
class OKFBundleInfo:
    """Represents a discovered OKF bundle."""

    name: str
    title: str
    description: str
    path: Path  # bundle root directory
    okf_version: str
    rank: int = 0


class OKFProviderRegistryService(Service):
    """Pure provider registry for OKF knowledge bundles.

    Owns conflict resolution (first-wins by rank) and consumer access.
    """

    def __init__(self, ctx: Context):
        super().__init__(ctx, "okf_registry")
        self.bundles: dict[str, OKFBundleInfo] = {}
        self.ctx.effect(self._cleanup)

    def register_bundle(self, bundle: OKFBundleInfo) -> bool:
        """Register a bundle. First-wins by rank for same-name bundles.

        Lower rank number = higher priority. Existing bundle wins ties.
        """
        existing = self.bundles.get(bundle.name)
        if existing is not None:
            if bundle.rank < existing.rank:
                self.bundles[bundle.name] = bundle
                return True
            return False  # Existing wins ties and higher-rank
        self.bundles[bundle.name] = bundle
        return True

    def get_bundle(self, name: str) -> OKFBundleInfo | None:
        return self.bundles.get(name)

    def list_bundles(self) -> list[OKFBundleInfo]:
        return sorted(self.bundles.values(), key=lambda b: b.name)

    def _cleanup(self) -> None:
        self.bundles.clear()
