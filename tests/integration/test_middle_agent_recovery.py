"""Killing a link mid-conversation: a middle agent's two sides recover independently."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.agents import Commander, Follower, Outcome
from src.agents.dual_role import DualRoleAgent, exchange_logger_for
from src.agents.hierarchy import Hierarchy
from src.decision import DecisionResult
from src.state import FollowerState
from src.transport import InMemoryTransport, TransportClosedError

CHAIN = Hierarchy(
    {
        "ground": None,
        "drone-cmd": "ground",
        "drone-a": "drone-cmd",
        "drone-b": "drone-cmd",
    }
)


def comply(*_):
    return DecisionResult(True)


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


class Setup:
    """ground (plain Commander) -> drone-cmd (DualRoleAgent) -> two drones."""

    def __init__(self, log_dir=None):
        self.ground_end, cmd_up = pair()
        self.down = {}
        self.drones = {}
        for drone in ("drone-a", "drone-b"):
            self.down[drone], end = pair()
            self.drones[drone] = Follower(
                drone,
                end,
                decision_engine=comply,
                state=FollowerState(parent_id="drone-cmd"),
            )
        self.ground = Commander("ground")
        self.ground.add_follower("drone-cmd", self.ground_end)
        self.middle = DualRoleAgent(
            "drone-cmd",
            CHAIN,
            parent_link=cmd_up,
            follower_links=dict(self.down),
            decision_engine=comply,
            exchange_log=(
                exchange_logger_for(CHAIN, "drone-cmd", log_dir, session_id="crash")
                if log_dir
                else None
            ),
        )
        self.middle.start()

    def down_order(self, drone, command="HOLD"):
        """The middle agent commands one drone; the drone answers."""
        with ThreadPoolExecutor(max_workers=1) as pool:
            handled = pool.submit(self.drones[drone].handle_once, timeout=2.0)
            result = self.middle.commander.issue(drone, command, timeout=2.0)
            handled.result(timeout=2.0)
        return result

    def up_order(self, command="HOLD"):
        """Ground commands the middle agent; the middle agent answers."""
        with ThreadPoolExecutor(max_workers=1) as pool:
            served = pool.submit(self.middle.serve_once, timeout=2.0)
            result = self.ground.issue("drone-cmd", command, timeout=2.0)
            served.result(timeout=2.0)
        return result

    def reconnect_ground(self):
        """The ground side restarts and opens a fresh link."""
        self.ground_end, cmd_up = pair()
        self.ground = Commander("ground")
        self.ground.add_follower("drone-cmd", self.ground_end)
        self.middle.replace_link("ground", cmd_up)

    def reconnect_drone(self, drone):
        self.down[drone], end = pair()
        self.drones[drone] = Follower(
            drone,
            end,
            decision_engine=comply,
            state=FollowerState(parent_id="drone-cmd"),
        )
        self.middle.replace_link(drone, self.down[drone])

    def close(self):
        self.middle.stop()


@pytest.fixture
def setup():
    s = Setup()
    yield s
    s.close()


def test_upstream_crash_does_not_stop_downstream(setup):
    setup.ground_end.close()  # ground crashes

    assert wait_for(lambda: setup.middle.link_is_down("ground"))
    with pytest.raises(TransportClosedError):
        setup.middle.serve_once(timeout=1.0)
    # The middle agent keeps commanding its drones.
    assert setup.down_order("drone-a").outcome is Outcome.COMPLY
    assert setup.down_order("drone-b").outcome is Outcome.COMPLY


def test_upstream_recovers_on_a_fresh_link(setup):
    setup.ground_end.close()
    assert wait_for(lambda: setup.middle.link_is_down("ground"))

    setup.reconnect_ground()

    assert not setup.middle.link_is_down("ground")
    assert setup.up_order().outcome is Outcome.COMPLY
    assert setup.down_order("drone-b").outcome is Outcome.COMPLY


def test_downstream_crash_affects_only_that_drone(setup):
    setup.down["drone-a"].close()  # the link to drone-a dies

    assert wait_for(lambda: setup.middle.link_is_down("drone-a"))
    started = time.monotonic()
    with pytest.raises(TransportClosedError):
        setup.middle.commander.issue("drone-a", "HOLD", timeout=2.0)
    assert time.monotonic() - started < 1.0  # fails fast, no hang
    assert setup.down_order("drone-b").outcome is Outcome.COMPLY
    assert setup.up_order().outcome is Outcome.COMPLY


def test_downstream_recovers_on_a_fresh_link(setup):
    setup.down["drone-a"].close()
    assert wait_for(lambda: setup.middle.link_is_down("drone-a"))

    setup.reconnect_drone("drone-a")

    assert setup.down_order("drone-a").outcome is Outcome.COMPLY


def test_replacing_a_live_link_retires_the_old_one(setup):
    old_ground = setup.ground_end
    setup.reconnect_ground()

    old_ground.send(
        {
            "message_id": "late-1",
            "sender_id": "ground",
            "recipient_id": "drone-cmd",
            "timestamp": "2026-11-13T12:00:00.000000Z",
            "schema_version": 1,
            "sender_role": "commander",
            "role_epoch": 0,
            "type": "command",
            "payload": {"command": "RETURN"},
        }
    )
    # Nothing from the retired link reaches the Follower module.
    assert setup.up_order().outcome is Outcome.COMPLY
    assert setup.middle.follower.state.current_command is None
    # The retired link closing later doesn't take the new one down.
    old_ground.close()
    time.sleep(0.2)
    assert not setup.middle.link_is_down("ground")
    assert setup.up_order().outcome is Outcome.COMPLY


def test_crashes_never_corrupt_the_exchange_log(tmp_path):
    s = Setup(log_dir=tmp_path)
    try:
        assert s.up_order().outcome is Outcome.COMPLY
        threads = [
            threading.Thread(target=s.ground_end.close),
            threading.Thread(target=s.down["drone-a"].close),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert s.down_order("drone-b").outcome is Outcome.COMPLY
    finally:
        s.close()

    lines = (tmp_path / "drone-cmd.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]  # every line is whole JSON
    assert len(records) == 4  # HOLD in, COMPLY out, HOLD out, COMPLY in
    assert all(r["record_type"] == "message" for r in records)
