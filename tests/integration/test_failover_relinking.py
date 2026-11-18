"""After a failover the agents re-link, and orders flow through the new commander.

Ground -> drone-cmd -> drone-a, drone-b, drone-c, every agent with heartbeats
and a Role Manager. Killing drone-cmd must end with the successor commanding
the other drones, Ground commanding the successor, and Ground still the top.
"""

import json
import threading
import time
from pathlib import Path

import pytest

from src.agents import Outcome
from src.agents.dual_role import DualRoleAgent, exchange_logger_for
from src.agents.hierarchy import Hierarchy
from src.roles.manager import Role, RoleManager
from src.state import FollowerState
from src.transport import InMemoryTransport, TransportClosedError, TransportError

SPONSOR = Hierarchy(
    {
        "ground": None,
        "drone-cmd": "ground",
        "drone-a": "drone-cmd",
        "drone-b": "drone-cmd",
        "drone-c": "drone-cmd",
    }
)
DRONES = ("drone-a", "drone-b", "drone-c")
# The storage tests ingest a committed copy of a failover's logs.
STORAGE_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "storage/dotnet/tests/FalconStorage.Tests/Fixtures/agent-logs/failover"
)
ROLE_KEYS = (
    "logged_by",
    "hierarchy_level",
    "requester_id",
    "target_commander_id",
    "previous_commander_id",
    "previous_role",
    "new_role",
    "old_epoch",
    "new_epoch",
    "outcome",
    "reason",
)
FIT = {"battery_percent": 90, "current_command": None}
INTERVAL = 0.05  # heartbeat interval; a loss is declared after 3 of them


def pair():
    a, b = InMemoryTransport.pair()
    a.connect()
    b.connect()
    return a, b


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class Fleet:
    """Every agent of a hierarchy, in memory, with a switchboard for new links."""

    def __init__(self, hierarchy, *, states=None, log_dir=None, session_id="relink"):
        states = states or {}
        self.down: set[str] = set()
        self.agents: dict[str, DualRoleAgent] = {}
        self._stop = {}
        self._threads = []
        ends = {
            child: pair() for child in hierarchy.agents if hierarchy.parent_of(child)
        }
        for agent in hierarchy.agents:
            parent = hierarchy.parent_of(agent)
            self.agents[agent] = DualRoleAgent(
                agent,
                hierarchy,
                parent_link=ends[agent][1] if parent else None,
                follower_links={
                    child: ends[child][0] for child in hierarchy.children_of(agent)
                },
                state=FollowerState(**states.get(agent, {})) if parent else None,
                relay_timeout=1.0,
                roles=RoleManager(agent, hierarchy, status=self._status_of(agent)),
                heartbeat_interval=INTERVAL,
                heartbeat_status=(lambda: dict(FIT)) if parent is None else None,
                connector=self._connector(agent),
                exchange_log=(
                    exchange_logger_for(
                        hierarchy, agent, log_dir, session_id=session_id
                    )
                    if log_dir
                    else None
                ),
            )

    def _status_of(self, agent):
        def status():
            built = self.agents.get(agent)
            if built is None or built.follower is None:
                return dict(FIT)
            return built.follower.status_snapshot()

        return status

    def _connector(self, me):
        def connect(peer):
            if peer in self.down:
                raise TransportClosedError(f"{peer} is down")
            mine, theirs = pair()
            self.agents[peer].accept_link(me, theirs)
            return mine

        return connect

    def _serve(self, agent, stop):
        while not stop.is_set():
            try:
                self.agents[agent].serve_once(timeout=INTERVAL)
            except TransportError:
                time.sleep(INTERVAL / 5)  # e.g. the old parent's link closed

    def start(self):
        for name, agent in self.agents.items():
            agent.start()
            if agent.follower is not None:
                stop = self._stop[name] = threading.Event()
                thread = threading.Thread(
                    target=self._serve, args=(name, stop), daemon=True
                )
                thread.start()
                self._threads.append(thread)

    def kill(self, *names):
        for name in names:
            self.down.add(name)
            if name in self._stop:
                self._stop[name].set()
            self.agents[name].stop()

    def stop(self):
        for event in self._stop.values():
            event.set()
        for agent in self.agents.values():
            agent.stop()
        for thread in self._threads:
            thread.join(2)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def roles(self, name) -> RoleManager:
        return self.agents[name].roles


def statuses_shared(fleet):
    """Every drone has heard every other drone's status (via drone-cmd's beats)."""
    return all(
        set(DRONES) - {drone} <= set(fleet.roles(drone).peer_status) for drone in DRONES
    )


def commanded_by(fleet, successor, followers):
    """The new chain is in place, in the Role Managers and in the running agents."""
    ground = fleet.agents["ground"]
    return (
        fleet.roles("ground").followers() == (successor,)
        and ground.commander.followers == [successor]
        and all(
            fleet.roles(f).get_current_commander() == successor
            and fleet.agents[f].hierarchy.parent_of(f) == successor
            for f in followers
        )
    )


def test_successor_takes_over_and_ground_commands_through_it(tmp_path):
    away = {drone: {"position": "north-fence"} for drone in DRONES}
    with Fleet(SPONSOR, states=away, log_dir=tmp_path) as fleet:
        assert wait_for(lambda: statuses_shared(fleet))
        fleet.kill("drone-cmd")

        assert wait_for(lambda: commanded_by(fleet, "drone-a", ("drone-b", "drone-c")))
        # Ground stays the top; everyone moved to the new epoch.
        assert fleet.roles("ground").role is Role.COMMANDER
        assert {fleet.roles(a).role_epoch for a in ("ground", *DRONES)} == {1}
        accepted = [r for r in fleet.roles("ground").records if r.get("outcome")]
        assert [r["outcome"] for r in accepted] == ["successor_accepted"]
        assert accepted[0]["requester_id"] == "drone-a"
        assert accepted[0]["previous_commander_id"] == "drone-cmd"

        # An order from Ground now goes through drone-a to the other drones.
        result = fleet.agents["ground"].commander.issue("drone-a", "RETURN", timeout=3)
        assert result.outcome is Outcome.COMPLY
        for drone in DRONES:
            assert fleet.agents[drone].follower.state.position == "base"
        relay = fleet.agents["drone-a"].relaying.last_relay
        assert sorted(relay.results) == ["drone-b", "drone-c"]

    # drone-a's log stamps its new level: 1, right under Ground.
    records = [
        json.loads(line)
        for line in (tmp_path / "drone-a.jsonl").read_text().splitlines()
    ]
    answers = [
        r
        for r in records
        if r.get("direction") == "sent"
        and r["message"]["recipient_id"] == "ground"
        and r["message"]["type"] == "response"
    ]
    assert answers and {r["hierarchy_level"] for r in answers} == {1}


def test_an_unfit_first_candidate_is_skipped():
    states = {"drone-a": {"battery_percent": 5}}
    with Fleet(SPONSOR, states=states) as fleet:
        assert wait_for(lambda: statuses_shared(fleet))
        fleet.kill("drone-cmd")

        assert wait_for(lambda: commanded_by(fleet, "drone-b", ("drone-a", "drone-c")))
        assert fleet.roles("ground").role is Role.COMMANDER
        result = fleet.agents["ground"].commander.issue("drone-b", "HOLD", timeout=3)
        assert result.outcome is Outcome.COMPLY


def test_a_successor_that_never_announces_is_passed_over():
    with Fleet(SPONSOR) as fleet:
        assert wait_for(lambda: statuses_shared(fleet))
        # drone-a, first in line, goes down together with its commander.
        fleet.kill("drone-cmd", "drone-a")

        assert wait_for(lambda: commanded_by(fleet, "drone-b", ("drone-c",)))
        assert fleet.roles("drone-c").role_epoch == 1
        result = fleet.agents["ground"].commander.issue("drone-b", "HOLD", timeout=3)
        assert result.outcome is Outcome.COMPLY


def run_failover(log_dir, session_id="relink"):
    """drone-cmd dies, drone-a takes over, and Ground orders a HOLD through it.

    Every agent logs to ``log_dir``. Used to make the storage fixture.
    """
    with Fleet(SPONSOR, log_dir=log_dir, session_id=session_id) as fleet:
        assert wait_for(lambda: statuses_shared(fleet))
        fleet.kill("drone-cmd")
        assert wait_for(lambda: commanded_by(fleet, "drone-a", ("drone-b", "drone-c")))
        result = fleet.agents["ground"].commander.issue("drone-a", "HOLD", timeout=3)
        assert result.outcome is Outcome.COMPLY


def role_records(log_dir):
    """Each agent's role-change records, as {agent: [record, ...]}."""
    found = {}
    for path in sorted(Path(log_dir).glob("*.jsonl")):
        records = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        ]
        changes = [r for r in records if r["record_type"] == "role_change"]
        if changes:
            found[path.stem] = changes
    return found


def test_failover_role_records_match_the_storage_fixture(tmp_path):
    # Each side of a failover logs its own role record: the successor that
    # promoted itself, and the commander above that accepted it. Storage must
    # know every outcome the agents write; if the records change, this fails
    # until the fixture is made again with run_failover.
    run_failover(tmp_path)
    live = role_records(tmp_path)
    fixture = role_records(STORAGE_FIXTURE)

    assert sorted(live) == sorted(fixture) == ["drone-a", "ground"]
    for agent, records in live.items():
        assert len(records) == len(fixture[agent])
        for fresh, fixed in zip(records, fixture[agent]):
            assert set(fresh) == set(fixed)
            assert {k: fresh[k] for k in ROLE_KEYS} == {k: fixed[k] for k in ROLE_KEYS}
    assert [r["outcome"] for r in live["drone-a"]] == ["self_promoted"]
    assert [r["outcome"] for r in live["ground"]] == ["successor_accepted"]


@pytest.mark.parametrize("depth", [4])
def test_failover_in_the_middle_of_a_deep_chain_keeps_the_top(depth):
    ids = [f"agent-{n}" for n in range(depth)]
    chain = Hierarchy.chain(ids)
    with Fleet(chain) as fleet:
        fleet.kill(ids[1])

        assert wait_for(
            lambda: fleet.agents[ids[0]].commander.followers == [ids[2]]
            and fleet.agents[ids[3]].hierarchy.parent_of(ids[3]) == ids[2]
        )
        assert fleet.roles(ids[0]).role is Role.COMMANDER
        assert fleet.roles(ids[3]).get_current_commander() == ids[2]
        result = fleet.agents[ids[0]].commander.issue(ids[2], "HOLD", timeout=3)
        assert result.outcome is Outcome.COMPLY
        assert fleet.agents[ids[3]].follower.state.movement_flag is False
