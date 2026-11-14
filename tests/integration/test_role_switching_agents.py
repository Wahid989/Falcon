"""Role switching between running agents over their links."""

import threading
import time

import pytest

from src.agents import Outcome
from src.agents.dual_role import DualRoleAgent, DualRoleError
from src.agents.hierarchy import Hierarchy
from src.roles.manager import Role, RoleManager
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
def agents():
    """ground -> drone-cmd -> drone-a, each with its own Role Manager."""
    ground_down, cmd_up = pair()
    cmd_down, a_up = pair()
    roles = {
        agent: RoleManager(agent, CHAIN, status=lambda: FIT) for agent in CHAIN.agents
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
        ),
        "drone-a": DualRoleAgent(
            "drone-a", CHAIN, parent_link=a_up, roles=roles["drone-a"]
        ),
    }
    for agent in built.values():
        agent.start()
    yield built
    for agent in built.values():
        agent.stop()
    for end in (ground_down, cmd_up, cmd_down, a_up):
        end.close()


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


def test_denied_request_over_the_link(agents):
    ground, middle = agents["ground"], agents["drone-cmd"]
    ground.roles.observe_status("drone-cmd", {**FIT, "terrain": "swamp"})
    ground.roles._eligibility = lambda status: (
        "terrain_unsuitable" if status.get("terrain") == "swamp" else None
    )

    middle.request_role()

    assert wait_for(lambda: middle.roles.last_deny == "terrain_unsuitable")
    assert middle.roles.role is Role.FOLLOWER
    assert ground.roles.role is Role.COMMANDER
    assert ground.commander.role_epoch == middle.commander.role_epoch == 0


def test_skip_level_needs_a_link_to_the_top(agents):
    with pytest.raises(DualRoleError, match="no link to 'ground'"):
        agents["drone-a"].request_role("ground")
    assert agents["drone-a"].roles.role is Role.FOLLOWER


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
