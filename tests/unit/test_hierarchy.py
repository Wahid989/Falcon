"""The command-chain model: relationships, traversal, depth and integrity."""

from pathlib import Path

import pytest

from src.agents.hierarchy import (
    AgentView,
    Hierarchy,
    HierarchyError,
    UnknownAgentError,
)
from src.state import FollowerState

GROUND_DRONE = (
    Path(__file__).resolve().parents[2] / "config" / "hierarchy" / "ground-drone.yml"
)


@pytest.fixture
def sponsor():
    """ground -> drone-cmd -> drone-a/b/c, and ground -> ground-agent."""
    return Hierarchy(
        {
            "ground": None,
            "drone-cmd": "ground",
            "ground-agent": "ground",
            "drone-a": "drone-cmd",
            "drone-b": "drone-cmd",
            "drone-c": "drone-cmd",
        }
    )


# --- parent / child relationships (from mission config) -------------------------


def test_loads_mission_config():
    hierarchy = Hierarchy.load(GROUND_DRONE)
    assert hierarchy.top == "ground"
    assert hierarchy.parent_of("drone-cmd") == "ground"
    assert hierarchy.children_of("drone-cmd") == ("drone-a", "drone-b", "drone-c")
    assert hierarchy.children_of("ground") == ("drone-cmd",)
    assert hierarchy.role_epoch == 0


def test_every_agent_resolves_parent_and_children(sponsor):
    assert sponsor.parent_of("ground") is None
    assert sponsor.parent_of("drone-a") == "drone-cmd"
    assert sponsor.children_of("ground") == ("drone-cmd", "ground-agent")
    assert sponsor.children_of("drone-a") == ()
    for agent in sponsor.agents:
        for child in sponsor.children_of(agent):
            assert sponsor.parent_of(child) == agent


def test_config_round_trip(sponsor):
    assert Hierarchy.from_config(sponsor.to_config()) == sponsor
    config = sponsor.to_config()
    assert config["agents"][0] == {"id": "ground"}
    # Parents come before their followers.
    order = [entry["id"] for entry in config["agents"]]
    for entry in config["agents"]:
        if "parent" in entry:
            assert order.index(entry["parent"]) < order.index(entry["id"])


def test_unknown_agent_is_a_clear_error(sponsor):
    with pytest.raises(UnknownAgentError, match="'nobody'"):
        sponsor.parent_of("nobody")
    assert "nobody" not in sponsor


# --- updates on handover --------------------------------------------------------


def test_moving_an_agent_moves_its_subtree(sponsor):
    moved = sponsor.with_parent("drone-cmd", "ground-agent")
    assert moved.parent_of("drone-cmd") == "ground-agent"
    assert moved.level_of("drone-cmd") == 2
    assert moved.level_of("drone-a") == 3
    assert sponsor.level_of("drone-a") == 2  # the original is unchanged


def test_skip_level_grant_makes_the_requester_the_new_top(sponsor):
    granted = sponsor.with_new_top("drone-a", role_epoch=1)

    assert granted.top == "drone-a"
    assert granted.role_epoch == 1
    assert granted.parent_of("ground") == "drone-a"
    # The previous top drops from level 0 to 1, and everything under it shifts.
    assert granted.level_of("ground") == 1
    assert granted.level_of("drone-cmd") == 2
    assert granted.level_of("drone-b") == 3
    assert "drone-a" not in granted.children_of("drone-cmd")


def test_grant_to_a_middle_agent_keeps_its_followers(sponsor):
    granted = sponsor.with_new_top("drone-cmd", role_epoch=1)
    assert granted.top == "drone-cmd"
    assert granted.children_of("drone-cmd") == (
        "drone-a",
        "drone-b",
        "drone-c",
        "ground",
    )
    assert granted.level_of("ground-agent") == 2


def test_grant_needs_a_newer_epoch(sponsor):
    with pytest.raises(HierarchyError, match="must be greater than 0"):
        sponsor.with_new_top("drone-a", role_epoch=0)


def test_adding_an_agent(sponsor):
    grown = sponsor.with_agent("drone-d", "drone-cmd")
    assert grown.children_of("drone-cmd")[-1] == "drone-d"
    assert grown.level_of("drone-d") == 2
    with pytest.raises(HierarchyError, match="already in the hierarchy"):
        sponsor.with_agent("drone-a", "ground")


# --- traversal ------------------------------------------------------------------


def test_traversal(sponsor):
    assert sponsor.top == "ground"
    assert sponsor.levels() == {
        "ground": 0,
        "drone-cmd": 1,
        "ground-agent": 1,
        "drone-a": 2,
        "drone-b": 2,
        "drone-c": 2,
    }
    assert sponsor.path_to_top("drone-b") == ("drone-b", "drone-cmd", "ground")
    assert sponsor.path_to_top("ground") == ("ground",)
    assert sponsor.subtree("drone-cmd") == (
        "drone-cmd",
        "drone-a",
        "drone-b",
        "drone-c",
    )
    assert sponsor.subtree("drone-a") == ("drone-a",)
    assert len(sponsor.subtree("ground")) == len(sponsor)
    assert sponsor.at_level(2) == ("drone-a", "drone-b", "drone-c")
    assert sponsor.depth == 3


def test_roles_by_position(sponsor):
    assert sponsor.is_top("ground")
    assert sponsor.is_middle("drone-cmd")
    assert not sponsor.is_middle("ground")
    assert sponsor.is_leaf("drone-a")
    assert not sponsor.is_leaf("drone-cmd")


def test_peer_levels_feed_the_exchange_log(sponsor):
    assert sponsor.peer_levels("drone-cmd") == {
        "ground": 0,
        "drone-a": 2,
        "drone-b": 2,
        "drone-c": 2,
    }
    assert sponsor.peer_levels("ground") == {"drone-cmd": 1, "ground-agent": 1}


def test_relation_of_peers(sponsor):
    assert sponsor.relation("drone-cmd", "ground") == "parent"
    assert sponsor.relation("drone-cmd", "drone-b") == "follower"
    assert sponsor.relation("drone-cmd", "ground-agent") is None  # a sibling
    assert sponsor.relation("drone-a", "ground") is None  # not a direct peer


# --- arbitrary depth ------------------------------------------------------------


@pytest.mark.parametrize("depth", [1, 2, 3, 5, 10, 50])
def test_chains_of_any_depth(depth):
    ids = [f"agent-{n}" for n in range(depth)]
    chain = Hierarchy.chain(ids)

    assert chain.depth == depth
    assert chain.top == "agent-0"
    for level, agent in enumerate(ids):
        assert chain.level_of(agent) == level
        assert chain.path_to_top(agent) == tuple(reversed(ids[: level + 1]))
    assert chain.is_leaf(ids[-1])
    assert all(chain.is_middle(agent) for agent in ids[1:-1])


@pytest.mark.parametrize("depth", [2, 3, 5])
def test_skip_level_grant_at_any_depth(depth):
    ids = [f"agent-{n}" for n in range(depth)]
    chain = Hierarchy.chain(ids)
    bottom = ids[-1]

    granted = chain.with_new_top(bottom, role_epoch=1)

    assert granted.top == bottom
    assert granted.depth == depth
    for level, agent in enumerate(ids[:-1]):
        assert granted.level_of(agent) == level + 1


def test_wide_tree_levels():
    parents = {"top": None}
    for branch in range(3):
        parents[f"b{branch}"] = "top"
        for leaf in range(4):
            parents[f"b{branch}-l{leaf}"] = f"b{branch}"
    tree = Hierarchy(parents)
    assert tree.depth == 3
    assert len(tree.at_level(2)) == 12


# --- integrity ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("parents", "message"),
    [
        ({"a": "a"}, "cannot be its own commander"),
        ({"a": "b", "b": "a"}, "circular hierarchy: a -> b -> a"),
        (
            {"top": None, "a": "c", "b": "a", "c": "b"},
            "circular hierarchy: a -> c -> b -> a",
        ),
        ({"a": None, "b": None}, "more than one top: a, b"),
        ({"a": None, "b": "ghost"}, "commander 'ghost' is not in the hierarchy"),
        ({}, "at least one agent"),
        ({"bad id": None}, "invalid agent id 'bad id'"),
        ({"a": None, "b": 7}, "parent must be an agent id"),
    ],
    ids=[
        "self",
        "two-cycle",
        "three-cycle",
        "two-tops",
        "unknown-parent",
        "empty",
        "bad-id",
        "bad-parent",
    ],
)
def test_invalid_structures_are_rejected(parents, message):
    with pytest.raises(HierarchyError) as error:
        Hierarchy(parents)
    assert message in str(error.value)


def test_every_problem_is_reported_at_once():
    with pytest.raises(HierarchyError) as error:
        Hierarchy({"a": None, "b": None, "c": "d", "d": "c"}, role_epoch=-1)
    problems = error.value.problems
    assert "role_epoch must be a whole number >= 0" in problems
    assert "more than one top: a, b" in problems
    assert "circular hierarchy: c -> d -> c" in problems


@pytest.mark.parametrize("epoch", [-1, 1.5, True, "0"])
def test_bad_epochs_are_rejected(epoch):
    with pytest.raises(HierarchyError, match="role_epoch"):
        Hierarchy({"a": None}, role_epoch=epoch)


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({}, "non-empty 'agents' list"),
        ({"agents": [{"parent": "x"}]}, "needs an 'id'"),
        ({"agents": [{"id": "a"}, {"id": "a"}]}, "listed more than once"),
        ({"agents": [{"id": "a", "level": 0}]}, "unknown keys: level"),
        ([], "must be a mapping"),
    ],
)
def test_bad_config_is_rejected(config, message):
    with pytest.raises(HierarchyError, match=message):
        Hierarchy.from_config(config)


def test_bad_chain_is_rejected():
    with pytest.raises(HierarchyError, match="more than once"):
        Hierarchy.chain(["a", "b", "a"])
    with pytest.raises(HierarchyError, match="at least one agent"):
        Hierarchy.chain([])


def test_moves_that_would_create_a_cycle_are_rejected(sponsor):
    with pytest.raises(HierarchyError, match="circular"):
        sponsor.with_parent("drone-cmd", "drone-a")


# --- agents' own views ----------------------------------------------------------


def views_matching(hierarchy, role_epoch=0):
    return {
        agent: AgentView(
            hierarchy.parent_of(agent),
            hierarchy.children_of(agent),
            "follower" if hierarchy.is_leaf(agent) else "commander",
            role_epoch,
        )
        for agent in hierarchy.agents
    }


def test_matching_views_pass(sponsor):
    assert sponsor.check_views(views_matching(sponsor)) == []
    sponsor.validate_views(views_matching(sponsor))


def test_view_from_follower_state():
    state = FollowerState(parent_id="drone-cmd", role_epoch=0)
    assert AgentView.of(state) == AgentView("drone-cmd", (), "follower", 0)


def test_mismatched_views_are_reported(sponsor):
    views = views_matching(sponsor)
    views["drone-a"] = AgentView("ground", (), "follower", 0)
    views["drone-cmd"] = AgentView("ground", ("drone-b", "drone-c"), "commander", 0)
    views["drone-b"] = AgentView("drone-cmd", (), "commander", 0)
    views["drone-c"] = AgentView("drone-cmd", (), "follower", 3)
    views["stranger"] = AgentView(None)

    problems = sponsor.check_views(views)

    assert "drone-a: thinks its commander is 'ground', expected 'drone-cmd'" in problems
    assert any(p.startswith("drone-cmd: thinks its followers are") for p in problems)
    assert "drone-b: claims to command but has no followers" in problems
    assert "drone-c: role_epoch 3 is newer than the hierarchy's 0" in problems
    assert "stranger: not in the hierarchy" in problems
    with pytest.raises(HierarchyError):
        sponsor.validate_views(views)


def test_a_follower_claimed_by_two_commanders_is_reported(sponsor):
    # A revived commander that still thinks it commands its old followers.
    views = views_matching(sponsor)
    views["ground-agent"] = AgentView("ground", ("drone-a",), "commander", 0)

    problems = sponsor.check_views(views)

    assert (
        "drone-a: claimed by more than one commander (drone-cmd, ground-agent)"
        in problems
    )
