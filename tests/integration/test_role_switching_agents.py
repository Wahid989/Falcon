"""Role switching between running agents over their links."""

import threading
import time

import pytest

from src.agents import Outcome
from src.agents.dual_role import DualRoleAgent, DualRoleError
from src.agents.hierarchy import Hierarchy
from src.roles.manager import Role, RoleError, RoleManager, basic_eligibility
from src.state import FollowerState
from src.transport import InMemoryTransport, TransportTimeoutError

CHAIN = Hierarchy({"ground": None, "drone-cmd": "ground", "drone-a": "drone-cmd"})
FIT = {"battery_percent": 90, "current_command": None}


def pair():
    a, b = InMemoryTransport.pair()
    a.connect()
    b.connect()
    return a, b


def wait_for(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def agents(request):
    """ground -> drone-cmd -> drone-a, each with its own Role Manager."""
    ground_down, cmd_up = pair()
    cmd_down, a_up = pair()
    roles = {
        agent: RoleManager(
            agent,
            CHAIN,
            eligibility=basic_eligibility(
                unsuitable_terrain={"swamp"}, required_capabilities={"radio"}
            ),
        )
        for agent in CHAIN.agents
    }
    built = {
        "ground": DualRoleAgent(
            "ground",
            CHAIN,
            follower_links={"drone-cmd": ground_down},
            roles=roles["ground"],
        ),
        "drone-cmd": DualRoleAgent(
            "drone-cmd",
            CHAIN,
            parent_link=cmd_up,
            follower_links={"drone-a": cmd_down},
            roles=roles["drone-cmd"],
            state=FollowerState(battery_percent=90, capabilities=["radio"]),
        ),
        "drone-a": DualRoleAgent(
            "drone-a",
            CHAIN,
            parent_link=a_up,
            roles=roles["drone-a"],
            state=FollowerState(battery_percent=90, capabilities=["radio"]),
        ),
    }
    try:
        for agent_id, agent in built.items():
            if agent_id != getattr(request, "param", None):
                agent.start()
        yield built
    finally:
        for agent in built.values():
            agent.stop()
        for end in (ground_down, cmd_up, cmd_down, a_up):
            end.close()


@pytest.fixture
def skip_link(agents):
    ground_link, leaf_link = pair()
    agents["ground"].accept_link("drone-a", ground_link)
    agents["drone-a"].accept_link("ground", leaf_link)
    try:
        yield leaf_link
    finally:
        ground_link.close()
        leaf_link.close()


def test_request_is_granted_over_the_link(agents):
    ground, middle = agents["ground"], agents["drone-cmd"]

    middle.request_role()

    assert wait_for(lambda: middle.roles.role is Role.COMMANDER)
    # Both managers agree, and both modules carry the new epoch.
    assert middle.roles.hierarchy == ground.roles.hierarchy
    assert ground.roles.get_current_commander() == "drone-cmd"
    assert middle.commander.role_epoch == ground.commander.role_epoch == 1
    assert middle.follower.state.role_epoch == 1
    # One-level propagation: drone-a was not told.
    assert agents["drone-a"].roles.role_epoch == 0


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ({"battery_percent": 10}, "degraded"),
        ({"mechanical_fault": True}, "degraded"),
        ({"current_command": "RETURN"}, "busy"),
        ({"terrain": "swamp"}, "terrain_unsuitable"),
        ({"capabilities": []}, "equipment_missing"),
    ],
)
def test_denied_request_over_the_link(agents, status, reason):
    ground, middle = agents["ground"], agents["drone-cmd"]
    ground.roles.observe_status(
        "drone-cmd", {**FIT, "capabilities": ["radio"], **status}
    )

    middle.request_role()

    assert wait_for(lambda: middle.roles.last_deny == reason)
    assert middle.roles.role is Role.FOLLOWER
    assert ground.roles.role is Role.COMMANDER
    assert ground.commander.role_epoch == middle.commander.role_epoch == 0


def test_request_checks_live_follower_state(agents):
    middle = agents["drone-cmd"]
    middle.follower.state.battery_percent = 10

    with pytest.raises(RoleError, match="eligibility check: degraded"):
        middle.request_role()

    assert middle.roles.role is Role.FOLLOWER
    assert agents["ground"].roles.role_epoch == 0

    middle.follower.state.battery_percent = 90
    middle.request_role()
    assert wait_for(lambda: middle.roles.role is Role.COMMANDER)


def test_intermediate_commander_denies_a_role_request(agents):
    middle, leaf = agents["drone-cmd"], agents["drone-a"]
    leaf.request_role("drone-cmd")

    assert wait_for(lambda: leaf.roles.last_deny == "not_authorized")
    assert leaf.roles.role is Role.FOLLOWER
    assert leaf.roles.role_epoch == middle.roles.role_epoch == 0
    assert middle.roles.get_current_commander() == "ground"


def test_skip_level_needs_a_link_to_the_top(agents):
    with pytest.raises(DualRoleError, match="no link to 'ground'"):
        agents["drone-a"].request_role("ground")
    assert agents["drone-a"].roles.role is Role.FOLLOWER


def test_skip_level_handover_nests_the_old_chain(agents, skip_link):
    ground, middle, leaf = (agents[name] for name in ("ground", "drone-cmd", "drone-a"))
    leaf.request_role()

    assert wait_for(lambda: leaf.commander.followers == ["ground"])
    assert leaf.roles.role is Role.COMMANDER
    assert leaf.roles.hierarchy == ground.roles.hierarchy
    assert leaf.roles.hierarchy.path_to_top("drone-cmd") == (
        "drone-cmd",
        "ground",
        "drone-a",
    )
    assert ground.roles.get_current_commander() == "drone-a"
    assert middle.roles.hierarchy == CHAIN
    assert middle.commander.role_epoch == middle.follower.state.role_epoch == 0
    assert leaf.roles.role_epoch == ground.roles.role_epoch == 1


def test_repeat_request_and_delayed_old_request_leave_handover_intact(
    agents, skip_link
):
    ground, leaf = agents["ground"], agents["drone-a"]
    request = leaf.request_role()
    assert wait_for(lambda: leaf.roles.role is Role.COMMANDER)

    with pytest.raises(RoleError, match="already the commander"):
        leaf.request_role()

    skip_link.send(request)
    assert wait_for(
        lambda: any(
            record.get("event_type") == "STALE_EPOCH_REJECTED"
            and record["detail"]["message_id"] == request["message_id"]
            for record in ground.roles.records
        )
    )
    assert leaf.roles.role is Role.COMMANDER
    assert ground.roles.get_current_commander() == "drone-a"
    assert leaf.roles.role_epoch == ground.roles.role_epoch == 1


@pytest.mark.parametrize("agents", ["ground"], indirect=True)
def test_overlapping_requests_install_exactly_one_commander(agents, skip_link):
    ground, middle, leaf = (agents[name] for name in ("ground", "drone-cmd", "drone-a"))
    requests = {agent.agent_id: agent.request_role() for agent in (middle, leaf)}
    assert middle.roles.role is leaf.roles.role is Role.TRANSITIONING

    ground.start()

    assert wait_for(
        lambda: all(
            agent.roles.role is not Role.TRANSITIONING for agent in agents.values()
        )
    )
    winners = [agent for agent in agents.values() if agent.roles.role is Role.COMMANDER]
    assert len(winners) == 1
    winner = winners[0]
    loser = leaf if winner is middle else middle
    assert ground.roles.get_current_commander() == winner.agent_id
    assert (
        winner.roles.role_epoch
        == loser.roles.role_epoch
        == ground.roles.role_epoch
        == 1
    )
    assert [record.get("outcome") for record in ground.roles.records].count(
        "granted"
    ) == 1
    assert any(
        record.get("event_type") == "STALE_EPOCH_REJECTED"
        and record["detail"]["message_id"] == requests[loser.agent_id]["message_id"]
        for record in ground.roles.records
    )


def test_revived_commander_is_rejected_and_steps_down():
    # Authority moved to epoch 1 while ground was away; ground still has 0.
    ground_down, cmd_up = pair()
    ground = DualRoleAgent(
        "ground",
        CHAIN,
        follower_links={"drone-cmd": ground_down},
        roles=RoleManager("ground", CHAIN),
    )
    middle_view = Hierarchy({"ground": None, "drone-cmd": "ground"}, role_epoch=1)
    middle = DualRoleAgent(
        "drone-cmd",
        middle_view,
        parent_link=cmd_up,
        state=FollowerState(movement_flag=True),
        roles=RoleManager("drone-cmd", middle_view),
    )
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                middle.serve_once(timeout=0.1)
            except TransportTimeoutError:
                continue

    server = threading.Thread(target=serve, daemon=True)
    with ground, middle:
        server.start()
        try:
            result = ground.commander.issue("drone-cmd", "HOLD", timeout=2.0)
        finally:
            stop.set()
            server.join(2)

    # The old commander's order is refused as stale...
    assert result.outcome is Outcome.ERROR
    assert result.error["code"] == "STALE_EPOCH"
    assert middle.follower.state.movement_flag is True  # HOLD was not applied
    stale = [r for r in middle.roles.records if r["record_type"] == "event"]
    assert stale[0]["event_type"] == "STALE_EPOCH_REJECTED"
    # ...and seeing the newer epoch, it steps down instead of reclaiming.
    assert ground.roles.role is Role.FOLLOWER
    assert ground.roles.role_epoch == 1
    assert ground.commander.role_epoch == 1
    changes = [r["outcome"] for r in ground.roles.records if "outcome" in r]
    assert changes == ["stepped_down"]
    ground_down.close()
    cmd_up.close()
