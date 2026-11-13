"""The deterministic failover rule: who takes over from a lost commander.

No voting. Every surviving agent applies the same rule to the same
information (the statuses shared through the commander's heartbeats), so they
all name the same successor:

1. The candidates are the lost commander's direct followers, sorted by agent
   id. If none of them fits, the search goes one level deeper (their
   followers, sorted by id).
2. The first candidate that passes the eligibility check is the successor.
   A candidate whose status isn't known is given the benefit of the doubt;
   its own live re-check at promotion is the real gate.
3. If no candidate fits, the role stays vacant until a human steps in.

See docs/failover.md.
"""

from __future__ import annotations

from typing import Callable, Iterable, Mapping

from src.agents.hierarchy import Hierarchy

Eligibility = Callable[[Mapping[str, object]], "str | None"]


def succession_candidates(hierarchy: Hierarchy, failed_id: str) -> list[str]:
    """Direct followers by id, then the next level down by id."""
    direct = sorted(hierarchy.children_of(failed_id))
    deeper = sorted(
        grandchild for child in direct for grandchild in hierarchy.children_of(child)
    )
    return direct + deeper


def choose_successor(
    hierarchy: Hierarchy,
    failed_id: str,
    *,
    statuses: Mapping[str, Mapping[str, object]],
    eligibility: Eligibility,
    excluded: Iterable[str] = (),
) -> str | None:
    """The agent that should take over, or None if nobody fits (vacant)."""
    skip = set(excluded)
    for candidate in succession_candidates(hierarchy, failed_id):
        if candidate in skip:
            continue
        status = statuses.get(candidate)
        if status is None or eligibility(status) is None:
            return candidate
    return None
