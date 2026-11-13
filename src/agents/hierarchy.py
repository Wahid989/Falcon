"""The command chain: who commands whom, at any depth.

A ``Hierarchy`` maps every agent to its parent (its commander), with exactly
one top. Each agent can resolve its parent, its followers, the top, its
distance from the top (``hierarchy_level``) and the subtree it commands. The
model is immutable: a structure change (a handover, a skip-level grant)
returns a new, validated ``Hierarchy``.

``hierarchy_level`` is computed here for logging only. It is never put on a
wire message.

The hierarchy is loaded from mission config at startup::

    role_epoch: 0
    agents:
      - id: ground
      - id: drone-cmd
        parent: ground
      - id: drone-a
        parent: drone-cmd
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from re import fullmatch
from typing import Iterable, Mapping, Sequence

import yaml

AGENT_ID = r"[A-Za-z0-9_-]+"


class HierarchyError(ValueError):
    """The structure is invalid. ``problems`` lists every rule it breaks."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("Invalid hierarchy: " + "; ".join(self.problems))


class UnknownAgentError(KeyError):
    """The agent is not part of this hierarchy."""

    def __str__(self) -> str:
        return f"Unknown agent {self.args[0]!r}"


@dataclass(frozen=True)
class AgentView:
    """What one agent believes about its own place in the chain.

    Built from that agent's state (``parent_id``, ``follower_ids``,
    ``current_role``, ``role_epoch``), so the agents' local views can be
    checked against the hierarchy and against each other.
    """

    parent_id: str | None
    follower_ids: tuple[str, ...] = ()
    current_role: str = "follower"
    role_epoch: int = 0

    @classmethod
    def of(cls, state) -> AgentView:
        """From any object with the follower-state fields."""
        return cls(
            state.parent_id,
            tuple(state.follower_ids),
            state.current_role,
            state.role_epoch,
        )


class Hierarchy:
    """An immutable, validated command chain."""

    __slots__ = ("_parents", "_children", "_levels", "_top", "role_epoch")

    def __init__(self, parents: Mapping[str, str | None], *, role_epoch: int = 0):
        problems = _structure_problems(parents, role_epoch)
        if problems:
            raise HierarchyError(problems)
        self._parents: dict[str, str | None] = dict(parents)
        self.role_epoch: int = role_epoch
        children: dict[str, list[str]] = {agent: [] for agent in self._parents}
        for agent, parent in self._parents.items():
            if parent is not None:
                children[parent].append(agent)
        self._children = {agent: tuple(sorted(c)) for agent, c in children.items()}
        self._top = next(a for a, p in self._parents.items() if p is None)
        self._levels = self._compute_levels()

    # --- building ------------------------------------------------------------

    @classmethod
    def from_config(cls, config: Mapping) -> Hierarchy:
        """From a mapping like ``{"role_epoch": 0, "agents": [{"id", "parent"}]}``."""
        if not isinstance(config, Mapping):
            raise HierarchyError(["config must be a mapping"])
        entries = config.get("agents")
        if not isinstance(entries, list) or not entries:
            raise HierarchyError(["config needs a non-empty 'agents' list"])

        problems: list[str] = []
        parents: dict[str, str | None] = {}
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping) or "id" not in entry:
                problems.append(f"agents[{index}] needs an 'id'")
                continue
            unknown = set(entry) - {"id", "parent"}
            if unknown:
                problems.append(
                    f"agents[{index}] has unknown keys: {', '.join(sorted(unknown))}"
                )
            agent = entry["id"]
            if agent in parents:
                problems.append(f"agent {agent!r} is listed more than once")
                continue
            parents[agent] = entry.get("parent")
        if problems:
            raise HierarchyError(problems)
        return cls(parents, role_epoch=config.get("role_epoch", 0))

    @classmethod
    def load(cls, path: str | Path) -> Hierarchy:
        """From a YAML mission config file."""
        with Path(path).open(encoding="utf-8") as handle:
            return cls.from_config(yaml.safe_load(handle) or {})

    @classmethod
    def chain(cls, agent_ids: Sequence[str], *, role_epoch: int = 0) -> Hierarchy:
        """A straight line: each agent commands the next one. Depth = len(ids)."""
        if not agent_ids:
            raise HierarchyError(["a chain needs at least one agent"])
        parents = {agent_ids[0]: None}
        for parent, agent in zip(agent_ids, agent_ids[1:]):
            if agent in parents:
                raise HierarchyError([f"agent {agent!r} is listed more than once"])
            parents[agent] = parent
        return cls(parents, role_epoch=role_epoch)

    def to_config(self) -> dict:
        """The mission-config form, parents before children."""
        ordered = sorted(self._parents, key=lambda a: (self._levels[a], a))
        agents = []
        for agent in ordered:
            entry = {"id": agent}
            if self._parents[agent] is not None:
                entry["parent"] = self._parents[agent]
            agents.append(entry)
        return {"role_epoch": self.role_epoch, "agents": agents}

    # --- looking things up ---------------------------------------------------

    @property
    def agents(self) -> tuple[str, ...]:
        return tuple(sorted(self._parents))

    def __contains__(self, agent_id: object) -> bool:
        return agent_id in self._parents

    def __len__(self) -> int:
        return len(self._parents)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Hierarchy):
            return NotImplemented
        return self._parents == other._parents and self.role_epoch == other.role_epoch

    def __hash__(self) -> int:
        return hash((frozenset(self._parents.items()), self.role_epoch))

    def __repr__(self) -> str:
        return f"Hierarchy(top={self._top!r}, agents={len(self)}, depth={self.depth})"

    @property
    def top(self) -> str:
        """The agent at level 0."""
        return self._top

    @property
    def depth(self) -> int:
        """Number of levels (a lone agent has depth 1)."""
        return max(self._levels.values()) + 1

    def parent_of(self, agent_id: str) -> str | None:
        """The agent's commander; ``None`` for the top."""
        return self._parents[self._known(agent_id)]

    def children_of(self, agent_id: str) -> tuple[str, ...]:
        """The agent's direct followers, sorted by id."""
        return self._children[self._known(agent_id)]

    def level_of(self, agent_id: str) -> int:
        """Distance from the top: 0 at the top, otherwise parent level + 1."""
        return self._levels[self._known(agent_id)]

    def levels(self) -> dict[str, int]:
        return dict(self._levels)

    def path_to_top(self, agent_id: str) -> tuple[str, ...]:
        """The agent, its parent, its parent's parent, ... up to the top."""
        path = [self._known(agent_id)]
        while self._parents[path[-1]] is not None:
            path.append(self._parents[path[-1]])
        return tuple(path)

    def subtree(self, agent_id: str) -> tuple[str, ...]:
        """The agent and everyone below it (its scope), parents first."""
        result: list[str] = []
        pending = [self._known(agent_id)]
        while pending:
            agent = pending.pop(0)
            result.append(agent)
            pending.extend(self._children[agent])
        return tuple(result)

    def is_top(self, agent_id: str) -> bool:
        return self._known(agent_id) == self._top

    def is_leaf(self, agent_id: str) -> bool:
        return not self.children_of(agent_id)

    def is_middle(self, agent_id: str) -> bool:
        """Has both a commander above and followers below (a dual-role agent)."""
        return not self.is_top(agent_id) and not self.is_leaf(agent_id)

    def at_level(self, level: int) -> tuple[str, ...]:
        return tuple(sorted(a for a, lv in self._levels.items() if lv == level))

    def peer_levels(self, agent_id: str) -> dict[str, int]:
        """Levels of the agent's direct peers (parent and followers).

        This is the known-peers table an agent's exchange log uses to stamp
        ``hierarchy_level`` on messages it receives.
        """
        parent = self.parent_of(agent_id)
        peers = list(self.children_of(agent_id))
        if parent is not None:
            peers.append(parent)
        return {peer: self._levels[peer] for peer in peers}

    def relation(self, agent_id: str, other_id: str) -> str | None:
        """``"parent"`` or ``"follower"`` for a direct peer, otherwise None."""
        if other_id == self.parent_of(agent_id):
            return "parent"
        if other_id in self.children_of(agent_id):
            return "follower"
        return None

    # --- changing the structure ----------------------------------------------

    def with_parent(self, agent_id: str, parent_id: str) -> Hierarchy:
        """Move an agent (and its subtree) under a new commander."""
        self._known(agent_id)
        parents = dict(self._parents)
        parents[agent_id] = parent_id
        return Hierarchy(parents, role_epoch=self.role_epoch)

    def with_new_top(self, agent_id: str, *, role_epoch: int) -> Hierarchy:
        """A granted (possibly skip-level) role request: the agent becomes the top.

        The agent keeps its own followers. The previous top, with everything
        else still under it, becomes a follower of the new top, so every
        agent outside the requester's subtree moves one level deeper. The
        grant must carry a newer epoch.
        """
        self._known(agent_id)
        if _is_epoch(role_epoch) and role_epoch <= self.role_epoch:
            raise HierarchyError(
                [f"new role_epoch {role_epoch} must be greater than {self.role_epoch}"]
            )
        if agent_id == self._top:
            return Hierarchy(self._parents, role_epoch=role_epoch)
        parents = dict(self._parents)
        parents[agent_id] = None
        parents[self._top] = agent_id
        return Hierarchy(parents, role_epoch=role_epoch)

    def after_failure(
        self, failed_id: str, successor_id: str, *, role_epoch: int
    ) -> Hierarchy:
        """The chain once ``successor_id`` takes over from a failed commander.

        The successor moves into the failed agent's place and keeps its own
        followers. The failed agent's other followers become the successor's.
        The failed agent stays in the chain as the successor's follower, so if
        it comes back it rejoins as a follower. The takeover needs a newer
        epoch.
        """
        self._known(failed_id)
        if successor_id not in self.subtree(failed_id)[1:]:
            raise HierarchyError(
                [f"{successor_id!r} is not below {failed_id!r}, so it can't succeed it"]
            )
        if _is_epoch(role_epoch) and role_epoch <= self.role_epoch:
            raise HierarchyError(
                [f"new role_epoch {role_epoch} must be greater than {self.role_epoch}"]
            )
        parents = dict(self._parents)
        parents[successor_id] = self._parents[failed_id]
        for follower in self._children[failed_id]:
            if follower != successor_id:
                parents[follower] = successor_id
        parents[failed_id] = successor_id
        return Hierarchy(parents, role_epoch=role_epoch)

    def with_agent(self, agent_id: str, parent_id: str) -> Hierarchy:
        """Add a new agent as a follower of ``parent_id``."""
        if agent_id in self._parents:
            raise HierarchyError([f"agent {agent_id!r} is already in the hierarchy"])
        return Hierarchy(
            {**self._parents, agent_id: parent_id}, role_epoch=self.role_epoch
        )

    # --- checking agents' own views ------------------------------------------

    def check_views(self, views: Mapping[str, AgentView]) -> list[str]:
        """Compare what each agent believes with this hierarchy.

        Returns a list of problems (empty when everything agrees):

        - an agent that isn't in the hierarchy;
        - a parent or follower list that differs from the hierarchy;
        - a follower claimed by more than one commander (one commander per
          scope);
        - a role that doesn't fit: an agent with no followers claiming to
          command, unless it is the top;
        - an epoch newer than the hierarchy's.
        """
        problems: list[str] = []
        claimed: dict[str, list[str]] = {}
        for agent in sorted(views):
            view = views[agent]
            if agent not in self._parents:
                problems.append(f"{agent}: not in the hierarchy")
                continue
            if view.parent_id != self._parents[agent]:
                problems.append(
                    f"{agent}: thinks its commander is {view.parent_id!r},"
                    f" expected {self._parents[agent]!r}"
                )
            if tuple(sorted(view.follower_ids)) != self._children[agent]:
                problems.append(
                    f"{agent}: thinks its followers are {sorted(view.follower_ids)},"
                    f" expected {list(self._children[agent])}"
                )
            for follower in view.follower_ids:
                claimed.setdefault(follower, []).append(agent)
            if (
                view.current_role == "commander"
                and not view.follower_ids
                and agent != self._top
            ):
                problems.append(f"{agent}: claims to command but has no followers")
            if not _is_epoch(view.role_epoch):
                problems.append(f"{agent}: role_epoch must be a whole number >= 0")
            elif view.role_epoch > self.role_epoch:
                problems.append(
                    f"{agent}: role_epoch {view.role_epoch} is newer than the"
                    f" hierarchy's {self.role_epoch}"
                )
        for follower, commanders in sorted(claimed.items()):
            if len(commanders) > 1:
                problems.append(
                    f"{follower}: claimed by more than one commander"
                    f" ({', '.join(commanders)})"
                )
        return problems

    def validate_views(self, views: Mapping[str, AgentView]) -> None:
        """Raise ``HierarchyError`` if ``check_views`` finds any problem."""
        problems = self.check_views(views)
        if problems:
            raise HierarchyError(problems)

    # --- internals -----------------------------------------------------------

    def _known(self, agent_id: str) -> str:
        if agent_id not in self._parents:
            raise UnknownAgentError(agent_id)
        return agent_id

    def _compute_levels(self) -> dict[str, int]:
        levels = {self._top: 0}
        pending = [self._top]
        while pending:
            agent = pending.pop()
            for child in self._children[agent]:
                levels[child] = levels[agent] + 1
                pending.append(child)
        return levels


def _is_epoch(value: object) -> bool:
    return type(value) is int and value >= 0


def _structure_problems(
    parents: Mapping[str, str | None], role_epoch: object
) -> list[str]:
    """Every structural rule the parent map breaks."""
    problems: list[str] = []
    if not _is_epoch(role_epoch):
        problems.append("role_epoch must be a whole number >= 0")
    if not isinstance(parents, Mapping) or not parents:
        return problems + ["the hierarchy needs at least one agent"]

    links: list[str] = []
    for agent, parent in parents.items():
        if not isinstance(agent, str) or fullmatch(AGENT_ID, agent) is None:
            links.append(f"invalid agent id {agent!r}")
        if parent is None:
            continue
        if not isinstance(parent, str):
            links.append(f"{agent}: parent must be an agent id, got {parent!r}")
        elif parent == agent:
            links.append(f"{agent}: an agent cannot be its own commander")
        elif parent not in parents:
            links.append(f"{agent}: commander {parent!r} is not in the hierarchy")
    if links:
        # Cycles and tops can't be worked out from broken links.
        return problems + links

    tops = sorted(agent for agent, parent in parents.items() if parent is None)
    if len(tops) > 1:
        problems.append(f"more than one top: {', '.join(tops)}")
    # With every parent known, "no top" always means a cycle, reported here.
    for cycle in _cycles(parents):
        problems.append("circular hierarchy: " + " -> ".join(cycle))
    return problems


def _cycles(parents: Mapping[str, str | None]) -> Iterable[list[str]]:
    """Each distinct cycle in the parent links, as agent ids ending where it began."""
    seen_in_cycle: set[str] = set()
    for start in sorted(parents):
        path: list[str] = []
        on_path: dict[str, int] = {}
        agent: str | None = start
        while agent is not None and agent not in on_path:
            if agent in seen_in_cycle:
                break
            on_path[agent] = len(path)
            path.append(agent)
            agent = parents[agent]
        if agent is not None and agent in on_path:
            cycle = path[on_path[agent] :]
            if not seen_in_cycle.intersection(cycle):
                seen_in_cycle.update(cycle)
                # Start each cycle at its smallest id so it's reported once.
                first = cycle.index(min(cycle))
                cycle = cycle[first:] + cycle[:first]
                yield cycle + [cycle[0]]
