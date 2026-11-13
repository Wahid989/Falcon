"""The sponsor's case end to end: Ground -> Drone Commander -> Drones A/B/C.

Every agent is a DualRoleAgent built from config/hierarchy/ground-drone.yml.
Ground talks only to the Drone Commander, which relays each command to its
drones and answers Ground with a summary. Runs over in-memory links and over
real TCP.
"""

import json
import threading
from pathlib import Path

import pytest

from src.agents import Outcome
from src.agents.dual_role import DualRoleAgent, exchange_logger_for
from src.agents.hierarchy import Hierarchy
from src.state import FollowerState
from src.transport import (
    InMemoryTransport,
    TCPClientTransport,
    TCPServerTransport,
    TransportClosedError,
    TransportTimeoutError,
)

CONFIG = (
    Path(__file__).resolve().parents[2] / "config" / "hierarchy" / "ground-drone.yml"
)
DRONES = ("drone-a", "drone-b", "drone-c")


def memory_link():
    parent_end, child_end = InMemoryTransport.pair()
    parent_end.connect()
    child_end.connect()
    return parent_end, child_end


def tcp_link():
    child_end = TCPServerTransport("127.0.0.1", 0, connect_timeout=5)
    port = child_end.listen()[1]
    accepting = threading.Thread(target=child_end.connect)
    accepting.start()
    parent_end = TCPClientTransport("127.0.0.1", port, connect_timeout=5)
    parent_end.connect()
    accepting.join(5)
    return parent_end, child_end


def moving(**overrides):
    return FollowerState(
        movement_flag=True,
        current_command="RETURN",
        position="north-fence",
        **overrides,
    )


class Network:
    """Every agent in a hierarchy, linked parent to child, serving its commander."""

    def __init__(
        self,
        hierarchy,
        make_link=memory_link,
        *,
        states=None,
        silent=(),
        relay_timeouts=None,
        log_dir=None,
    ):
        states = states or {}
        relay_timeouts = relay_timeouts or {}
        self.links = []
        up, down = {}, {}
        for agent in hierarchy.agents:
            for child in hierarchy.children_of(agent):
                parent_end, child_end = make_link()
                self.links += [parent_end, child_end]
                down.setdefault(agent, {})[child] = parent_end
                up[child] = child_end
        self.agents = {
            agent: DualRoleAgent(
                agent,
                hierarchy,
                parent_link=up.get(agent),
                follower_links=down.get(agent, {}),
                state=states.get(agent, moving()),
                relay_timeout=relay_timeouts.get(agent, 1.0),
                exchange_log=(
                    exchange_logger_for(hierarchy, agent, log_dir, session_id="sc")
                    if log_dir
                    else None
                ),
            )
            for agent in hierarchy.agents
        }
        self._stop = threading.Event()
        self._servers = [
            threading.Thread(target=self._serve, args=(agent,), daemon=True)
            for name, agent in self.agents.items()
            if agent.follower is not None and name not in silent
        ]

    def _serve(self, agent):
        while not self._stop.is_set():
            try:
                agent.serve_once(timeout=0.1)
            except TransportTimeoutError:
                continue
            except TransportClosedError:
                return

    def __enter__(self):
        for agent in self.agents.values():
            agent.start()
        for server in self._servers:
            server.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        for server in self._servers:
            server.join(2)
        for agent in self.agents.values():
            agent.stop()
        for link in self.links:
            link.close()

    def state(self, agent):
        return self.agents[agent].follower.state


@pytest.fixture
def sponsor():
    return Hierarchy.load(CONFIG)


@pytest.fixture(params=[memory_link, tcp_link], ids=["memory", "tcp"])
def make_link(request):
    return request.param


def test_hold_flows_down_and_comply_flows_back_up(sponsor, make_link):
    with Network(sponsor, make_link) as net:
        ground = net.agents["ground"].commander
        result = ground.issue("drone-cmd", "HOLD", timeout=5.0)

        assert result.outcome is Outcome.COMPLY
        assert ground.followers == ["drone-cmd"]  # Ground never talks to drones
        relay = net.agents["drone-cmd"].relaying.last_relay
        assert {fid: relay.outcome_of(fid) for fid in DRONES} == dict.fromkeys(
            DRONES, "COMPLY"
        )
        for agent in ("drone-cmd", *DRONES):
            assert net.state(agent).movement_flag is False


def test_a_drone_decline_comes_back_up_with_its_reason(sponsor, make_link):
    states = {"drone-b": moving(battery_percent=5)}
    with Network(sponsor, make_link, states=states) as net:
        before = net.state("drone-b")
        result = net.agents["ground"].commander.issue("drone-cmd", "RETURN", timeout=5)

        assert result.outcome is Outcome.DECLINE
        assert result.reason == "BATTERY_CRITICAL"
        assert net.state("drone-b") == before
        # The others had already complied; there is no undo.
        assert net.state("drone-a").position == "base"
        assert net.state("drone-c").position == "base"


def test_drone_commander_declining_itself_relays_nothing(sponsor):
    states = {"drone-cmd": moving(battery_percent=5)}
    with Network(sponsor, states=states) as net:
        result = net.agents["ground"].commander.issue("drone-cmd", "RETURN", timeout=5)

        assert result.outcome is Outcome.DECLINE
        assert result.reason == "BATTERY_CRITICAL"
        assert net.agents["drone-cmd"].relaying.last_relay is None
        for drone in DRONES:
            assert net.state(drone).position == "north-fence"


def test_waypoint_is_passed_down(sponsor):
    with Network(sponsor) as net:
        result = net.agents["ground"].commander.issue(
            "drone-cmd", "PROCEED_TO_WAYPOINT", waypoint="ridge", timeout=5
        )
        assert result.outcome is Outcome.COMPLY
        for drone in DRONES:
            assert net.state(drone).position == "ridge"


def test_status_is_answered_by_the_drone_commander_itself(sponsor):
    with Network(sponsor) as net:
        result = net.agents["ground"].commander.issue(
            "drone-cmd", "REPORT_STATUS", timeout=5
        )
        assert result.outcome is Outcome.STATUS_RESPONSE
        assert result.state["agent_id"] == "drone-cmd"
        assert net.agents["drone-cmd"].relaying.last_relay is None


def test_a_silent_drone_means_no_answer_upward(sponsor):
    relay_timeouts = {"drone-cmd": 0.5}
    with Network(sponsor, silent={"drone-c"}, relay_timeouts=relay_timeouts) as net:
        result = net.agents["ground"].commander.issue("drone-cmd", "HOLD", timeout=2)

        assert result.outcome is Outcome.TIMEOUT
        (incomplete,) = net.agents["drone-cmd"].incomplete
        assert incomplete.relay.unanswered() == {"drone-c": "TIMEOUT"}
        assert "drone-c (TIMEOUT)" in str(incomplete)
        # The drones that answered still got the command.
        assert net.state("drone-a").movement_flag is False
        assert net.state("drone-b").movement_flag is False
        # The drone commander did not take the command itself.
        assert net.state("drone-cmd").movement_flag is True


def test_every_agent_logs_its_part_at_its_level(sponsor, tmp_path):
    with Network(sponsor, log_dir=tmp_path) as net:
        net.agents["ground"].commander.issue("drone-cmd", "HOLD", timeout=5)

    def records(agent):
        path = tmp_path / f"{agent}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    ground = records("ground")
    assert {
        r["message"]["recipient_id"] for r in ground if r["direction"] == "sent"
    } == {"drone-cmd"}
    middle = records("drone-cmd")
    # HOLD in + 3 relayed out + 3 answers in + COMPLY out.
    assert len(middle) == 8
    for record in middle:
        sender = record["message"]["sender_id"]
        expected = {"ground": 0, "drone-cmd": 1}.get(sender, 2)
        assert record["hierarchy_level"] == expected
    for drone in DRONES:
        peers = {
            r["message"]["sender_id"]
            for r in records(drone)
            if r["direction"] == "received"
        }
        assert peers == {"drone-cmd"}


@pytest.mark.parametrize("depth", [3, 5])
def test_command_reaches_the_bottom_of_a_deep_chain(depth):
    ids = [f"agent-{n}" for n in range(depth)]
    chain = Hierarchy.chain(ids)
    # Each level waits less than the level above it.
    timeouts = {agent: 1.0 * (depth - level) for level, agent in enumerate(ids)}
    with Network(chain, relay_timeouts=timeouts) as net:
        result = net.agents[ids[0]].commander.issue(
            ids[1], "HOLD", timeout=1.0 * depth + 2
        )

        assert result.outcome is Outcome.COMPLY
        for agent in ids[1:]:
            assert net.state(agent).movement_flag is False


def test_deep_chain_decline_at_the_bottom_reaches_the_top():
    ids = [f"agent-{n}" for n in range(4)]
    chain = Hierarchy.chain(ids)
    states = {ids[-1]: moving(battery_percent=5)}
    timeouts = {agent: 1.0 * (4 - level) for level, agent in enumerate(ids)}
    with Network(chain, states=states, relay_timeouts=timeouts) as net:
        result = net.agents[ids[0]].commander.issue(ids[1], "RETURN", timeout=6)

        assert result.outcome is Outcome.DECLINE
        assert result.reason == "BATTERY_CRITICAL"
        # Each middle agent passed the decline up and stayed where it was.
        for agent in ids[1:]:
            assert net.state(agent).position == "north-fence"
