"""Shared terrain and equipment checks for command and role policies."""

from enum import Enum
from typing import Iterable


class SuitabilityFailure(Enum):
    TERRAIN = "terrain"
    EQUIPMENT = "equipment"


def check_suitability(
    terrain: object,
    capabilities: Iterable[str],
    *,
    unsuitable_terrain: frozenset[str],
    required_capabilities: frozenset[str],
) -> SuitabilityFailure | None:
    """Check configured restrictions using exact matches."""
    if isinstance(terrain, str) and terrain in unsuitable_terrain:
        return SuitabilityFailure.TERRAIN
    if not required_capabilities.issubset(capabilities):
        return SuitabilityFailure.EQUIPMENT
    return None
