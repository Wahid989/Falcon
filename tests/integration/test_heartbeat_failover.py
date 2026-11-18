"""Live idle heartbeats feed RoleManager and trigger loss only after its deadline."""

from time import monotonic, sleep
from unittest.mock import Mock

from src.agents.dual_role import DualRoleAgent
from src.agents.hierarchy import Hierarchy
from src.roles.manager import Role, RoleManager
from src.state import FollowerState
from src.transport import InMemoryTransport


def wait_for(predicate):
    deadline = monotonic() + 2
    while monotonic() < deadline:
        if predicate():
            return True
        sleep(0.005)
    return False


def test_live_beats_forward_status_then_parent_loss_calls_existing_failover():
    hierarchy = Hierarchy({"ground": None, "agent-a": "ground"})
    down, up = InMemoryTransport.pair()
    down.connect()
    up.connect()
    fit = {"battery_percent": 90, "current_command": None}
    ground_roles = RoleManager("ground", hierarchy, status=lambda: fit)
    leaf_roles = RoleManager("agent-a", hierarchy, status=lambda: fit)
    leaf_roles.on_commander_lost = Mock(wraps=leaf_roles.on_commander_lost)
    top = DualRoleAgent(
        "ground",
        hierarchy,
        follower_links={"agent-a": down},
        roles=ground_roles,
        heartbeat_interval=0.03,
        heartbeat_status=lambda: fit,
    )
    leaf = DualRoleAgent(
        "agent-a",
        hierarchy,
        parent_link=up,
        roles=leaf_roles,
        heartbeat_interval=0.03,
        state=FollowerState(movement_flag=True, current_command="RETURN"),
    )
    try:
        top.start()
        leaf.start()
        assert wait_for(lambda: "agent-a" in ground_roles.peer_status)
        # Several intervals pass with a live commander: there is no election.
        sleep(0.15)
        leaf_roles.on_commander_lost.assert_not_called()
        assert leaf._views["ground"]._inbox.empty()  # beats consumed by runtime
        top.stop()
        down.close()
        assert wait_for(lambda: leaf_roles.role is Role.COMMANDER)
        leaf_roles.on_commander_lost.assert_called_once_with("ground")
        assert leaf.follower.state.movement_flag is False
        assert leaf.follower.state.current_command is None
        assert leaf.commander.role_epoch == 1
    finally:
        top.stop()
        leaf.stop()
        down.close()
        up.close()
