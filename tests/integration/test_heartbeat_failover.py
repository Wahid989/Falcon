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


def test_tcp_eof_holds_before_the_heartbeat_election_deadline():
    from src.transport import TCPClientTransport, TCPServerTransport

    hierarchy = Hierarchy({"ground": None, "agent-a": "ground"})
    server = TCPServerTransport()
    client = TCPClientTransport(*server.listen())
    client.connect()
    server.connect()
    roles = RoleManager("agent-a", hierarchy, status=lambda: {"battery_percent": 90})
    roles.on_commander_lost = Mock(wraps=roles.on_commander_lost)
    leaf = DualRoleAgent(
        "agent-a",
        hierarchy,
        parent_link=client,
        roles=roles,
        heartbeat_interval=0.2,
        heartbeat_misses=3,
        state=FollowerState(
            position="gate", movement_flag=True, current_command="RETURN"
        ),
    )
    try:
        leaf.start()
        server.close()
        assert wait_for(lambda: leaf.link_is_down("ground"))
        assert leaf.follower.state.position == "gate"
        assert leaf.follower.state.movement_flag is False
        assert leaf.follower.state.current_command is None
        roles.on_commander_lost.assert_not_called()
        assert wait_for(lambda: roles.role is Role.COMMANDER)
        roles.on_commander_lost.assert_called_once_with("ground")
    finally:
        leaf.stop()
        server.close()
        client.close()


def test_late_inflight_movement_cannot_undo_disconnect_hold():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import pytest
    from src.agents import Commander
    from src.decision import DecisionResult
    from src.transport import TransportClosedError

    hierarchy = Hierarchy({"ground": None, "agent-a": "ground"})
    down, up = InMemoryTransport.pair()
    down.connect()
    up.connect()
    evaluating, release = Event(), Event()

    def delayed_policy(*args):
        evaluating.set()
        assert release.wait(2)
        return DecisionResult(True)

    roles = RoleManager("agent-a", hierarchy, status=lambda: {"battery_percent": 90})
    leaf = DualRoleAgent(
        "agent-a",
        hierarchy,
        parent_link=up,
        roles=roles,
        heartbeat_interval=5,
        decision_engine=delayed_policy,
    )
    try:
        leaf.start()
        request = Commander("ground").build_command(
            "agent-a", "PROCEED_TO_WAYPOINT", waypoint="unsafe-late-target"
        )
        down.send(request)
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(leaf.serve_once, 1)
            assert evaluating.wait(1)
            down.close()
            assert wait_for(lambda: leaf.link_is_down("ground"))
            release.set()
            with pytest.raises(TransportClosedError):
                result.result(timeout=2)
        assert leaf.follower.state.position == "base"
        assert leaf.follower.state.movement_flag is False
        assert leaf.follower.state.current_command is None
    finally:
        release.set()
        leaf.stop()
        down.close()
        up.close()


def test_silent_parent_is_detected_even_when_tcp_connection_stays_open():
    from src.transport import TCPClientTransport, TCPServerTransport

    hierarchy = Hierarchy({"ground": None, "agent-a": "ground"})
    server = TCPServerTransport()
    client = TCPClientTransport(*server.listen())
    client.connect()
    server.connect()
    roles = RoleManager("agent-a", hierarchy, status=lambda: {"battery_percent": 90})
    roles.on_commander_lost = Mock(wraps=roles.on_commander_lost)
    leaf = DualRoleAgent(
        "agent-a",
        hierarchy,
        parent_link=client,
        roles=roles,
        heartbeat_interval=0.04,
        heartbeat_misses=3,
        state=FollowerState(movement_flag=True, current_command="RETURN"),
    )
    try:
        started = monotonic()
        leaf.start()
        assert wait_for(lambda: roles.role is Role.COMMANDER)
        assert 0.1 <= monotonic() - started < 1
        assert not leaf.link_is_down("ground")  # No EOF was involved.
        roles.on_commander_lost.assert_called_once_with("ground")
        assert leaf.follower.state.movement_flag is False
        assert leaf.follower.state.current_command is None
    finally:
        leaf.stop()
        client.close()
        server.close()
