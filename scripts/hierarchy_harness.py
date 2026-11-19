"""Run the standalone FALCON-066–071 hierarchy acceptance scenarios."""

import argparse
import json
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from src.agents import Outcome
from src.agents.dual_role import DualRoleAgent, exchange_logger_for
from src.agents.heartbeat import HeartbeatMonitor
from src.agents.hierarchy import Hierarchy
from src.roles.manager import Role, RoleManager
from src.state import FollowerState
from src.transport import InMemoryTransport, TransportTimeoutError

DEFAULT_SCENARIO = Path(__file__).resolve().parents[1] / "tests/scenarios/hierarchy.yml"


class Fleet:
    """Drive the real agent routing over paired links with explicit delivery."""

    def __init__(self, depth, extra_followers, states, log_dir):
        self.chain = [f"agent-{index:02}" for index in range(depth)]
        parents = dict(zip(self.chain, [None, *self.chain[:-1]]))
        for index in range(extra_followers):
            parents[f"standby-{index:02}"] = self.chain[-2]
        self.initial = Hierarchy(parents)
        self.links = {}
        self.agents = {}
        self.offline = set()
        self.log_dir = log_dir
        for name in self.initial.agents:
            parent = self.initial.parent_of(name)
            if parent is not None:
                self.open_link(parent, name)
        for name in self.initial.agents:
            parent = self.initial.parent_of(name)
            self.agents[name] = DualRoleAgent(
                name,
                self.initial,
                parent_link=self.links[name, parent] if parent else None,
                follower_links={
                    child: self.links[name, child]
                    for child in self.initial.children_of(name)
                },
                roles=RoleManager(name, self.initial),
                state=FollowerState(**states.get(name, {})),
                heartbeat_interval=5,
                heartbeat_status=(lambda: {}) if parent is None else None,
                connector=lambda peer, owner=name: self.connect(owner, peer),
                exchange_log=exchange_logger_for(
                    self.initial, name, log_dir, session_id="hierarchy"
                ),
            )
        self.check_scopes(self.initial)

    def open_link(self, owner, peer):
        left, right = InMemoryTransport.pair()
        left.connect()
        right.connect()
        self.links[owner, peer], self.links[peer, owner] = left, right
        return left, right

    def connect(self, owner, peer):
        left, right = self.open_link(owner, peer)
        self.agents[peer].accept_link(owner, right)
        return left

    def drain(self):
        """Deliver queued envelopes in a stable order until the system settles."""
        for _ in range(100):
            delivered = False
            for (target, peer), link in tuple(self.links.items()):
                if target in self.offline:
                    continue
                try:
                    message = link.receive(timeout=0)
                except TransportTimeoutError:
                    continue
                self.agents[target].dispatch(message, peer)
                self.check_no_overlap()
                delivered = True
            if not delivered:
                return
        raise AssertionError("Hierarchy messages did not settle")

    def handover(self, requester):
        top = self.initial.top
        if (requester, top) not in self.links:
            self.agents[requester].accept_link(top, self.connect(requester, top))
        self.agents[requester].request_role(top)
        self.drain()
        hierarchy = self.agents[requester].roles.hierarchy
        assert hierarchy.top == requester
        assert hierarchy.role_epoch == 1
        assert hierarchy.parent_of(top) == requester
        assert self.agents[top].roles.hierarchy == hierarchy
        self.check_scopes(hierarchy)
        return hierarchy

    def lose(self, failed):
        self.offline.add(failed)
        # All direct survivors receive the same confirmed loss event.
        announcements = {}
        for name in self.initial.children_of(failed):
            agent = self.agents[name]
            agent._hold_on_loss()
            announcements[name] = agent.roles.on_commander_lost(failed)
        for name, messages in announcements.items():
            agent = self.agents[name]
            agent._sync_epoch()
            agent._send_all(agent._after_change(messages))
        self.drain()
        promotions = [
            name
            for name, agent in self.agents.items()
            if any(
                record.get("outcome") == "self_promoted"
                for record in agent.roles.records
            )
        ]
        assert promotions == [min(self.initial.children_of(failed))]
        hierarchy = self.agents[promotions[0]].roles.hierarchy
        assert hierarchy.role_epoch == 1
        assert hierarchy.parent_of(failed) == promotions[0]
        self.check_scopes(hierarchy)
        return promotions[0], hierarchy

    def check_scopes(self, expected):
        live = {
            name: agent
            for name, agent in self.agents.items()
            if name not in self.offline
        }
        assert [name for name, a in live.items() if a.roles.role is Role.COMMANDER] == [
            expected.top
        ]
        for child, follower in live.items():
            parent = expected.parent_of(child)
            assert follower.roles.get_current_commander() == parent, child
            # Bounded handover leaves some peers at their previous epoch. Their
            # obsolete routes cannot claim children at a newer epoch.
            authorities = [
                name
                for name, commander in live.items()
                if child in commander.commander.followers
                and commander.roles.role_epoch >= follower.roles.role_epoch
            ]
            assert authorities == ([parent] if parent else []), (child, authorities)

    def check_no_overlap(self):
        """Check each delivery, allowing old-epoch views during propagation."""
        live = [
            agent for name, agent in self.agents.items() if name not in self.offline
        ]
        epoch = max(agent.roles.role_epoch for agent in live)
        tops = [
            agent.agent_id
            for agent in live
            if agent.roles.role is Role.COMMANDER and agent.roles.role_epoch == epoch
        ]
        assert len(tops) <= 1, tops
        for follower in live:
            claims = [
                agent.roles.role_epoch
                for agent in live
                if follower.agent_id in agent.commander.followers
            ]
            if claims:
                assert claims.count(max(claims)) == 1, (follower.agent_id, claims)

    def check_forced_state(self, name, settings):
        """Read back the YAML-driven state through Commander and Follower."""
        parent = self.initial.parent_of(name)
        commander = self.agents[parent].commander
        command = commander.send_command(name, "REPORT_STATUS")
        self.drain()
        self.agents[name].serve_once(timeout=0)
        self.drain()
        result = commander.await_response(command, timeout=0)
        assert result.outcome is Outcome.STATUS_RESPONSE
        state = result.response["payload"]["state"]
        for field, value in settings.items():
            if field in state:
                assert state[field] == value, (name, field, state[field], value)

    def reject_delayed_order(self, previous, order):
        target = self.agents[order["recipient_id"]]
        state = target.follower.status_snapshot()
        routed_to_follower = target.route(order, previous) == "follower"
        self.links[previous, target.agent_id].send(order)
        self.drain()
        if routed_to_follower:
            target.serve_once(timeout=0)
            self.drain()
        assert target.follower.status_snapshot() == state
        assert any(
            record.get("event_type") == "STALE_EPOCH_REJECTED"
            and record["detail"]["message_id"] == order["message_id"]
            for record in target.roles.records
        )
        replies = [
            record
            for record in self.records(target.agent_id)
            if record.get("direction") == "sent"
            and record["message"]["payload"].get("in_reply_to") == order["message_id"]
            and record["message"]["payload"].get("error", {}).get("code")
            == "STALE_EPOCH"
        ]
        assert len(replies) == 1, order
        assert replies[0]["hierarchy_level"] == target.roles.hierarchy.level_of(
            target.agent_id
        )
        assert "hierarchy_level" not in replies[0]["message"]

    def records(self, name):
        path = self.log_dir / f"{name}.jsonl"
        self.agents[name]._exchange_log.flush()
        return [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def close(self):
        for agent in self.agents.values():
            agent.stop()
            agent._exchange_log.close()
        for link in self.links.values():
            link.close()


@contextmanager
def running_fleet(depth, extra_followers, states):
    with TemporaryDirectory(prefix="falcon-hierarchy-") as directory:
        fleet = Fleet(depth, extra_followers, states, Path(directory))
        try:
            yield fleet
        finally:
            fleet.close()


def scenarios(depth, extra_followers, states):
    with running_fleet(depth, extra_followers, states) as fleet:
        root, first = fleet.chain[:2]
        for name, settings in states.items():
            if name in fleet.initial and name != root:
                fleet.check_forced_state(name, settings)
        before = fleet.agents[root].commander.build_command(first, "HOLD")
        fleet.handover(first)
        fleet.reject_delayed_order(root, before)

    with running_fleet(depth, extra_followers, states) as fleet:
        root, leaf = fleet.chain[0], fleet.chain[-1]
        hierarchy = fleet.handover(leaf)
        for name in fleet.initial.agents:
            if name not in (root, leaf):
                assert hierarchy.parent_of(name) == fleet.initial.parent_of(name)
                assert fleet.agents[name].roles.role_epoch == 0
        # A real exchange emitted by the previous top verifies the log-only
        # level after insertion. The new top denies the return request.
        fleet.agents[leaf].roles.observe_status(root, {"battery_percent": 5})
        request = fleet.agents[root].request_role()
        fleet.drain()
        exchange = [
            record
            for record in fleet.records(root)
            if record.get("direction") == "sent"
            and record["message"]["message_id"] == request["message_id"]
        ]
        assert fleet.initial.level_of(root) == 0
        assert [record["hierarchy_level"] for record in exchange] == [1]
        assert "hierarchy_level" not in request
        fleet.check_scopes(hierarchy)

    with running_fleet(depth, extra_followers, states) as fleet:
        failed, successor = fleet.chain[-2:]
        before = fleet.agents[failed].commander.build_command(successor, "HOLD")
        actual, hierarchy = fleet.lose(failed)
        assert actual == successor
        assert hierarchy.level_of(actual) == fleet.initial.level_of(failed)
        fleet.reject_delayed_order(failed, before)

    with running_fleet(depth, extra_followers, states) as fleet:
        root = fleet.initial.top
        active_parent = fleet.chain[1]
        delayed = (
            fleet.agents[active_parent].commander.build_command(fleet.chain[2], "HOLD")
            if depth > 2
            else None
        )
        successor, hierarchy = fleet.lose(root)
        if delayed is not None:
            fleet.reject_delayed_order(active_parent, delayed)
        fleet.offline.remove(root)
        promotion = fleet.agents[successor]
        # The replacement opens a fresh link and sends its real role heartbeat.
        promotion.accept_link(root, fleet.connect(successor, root))
        now = 0
        monitor = HeartbeatMonitor(
            promotion.roles,
            promotion.follower.status_snapshot,
            promotion._validate,
            clock=lambda: now,
        )
        now = monitor.interval
        promotion._send_all(monitor.poll())
        fleet.drain()
        assert fleet.agents[root].roles.get_current_commander() == successor
        assert fleet.agents[root].roles.role is Role.FOLLOWER
        fleet.check_scopes(hierarchy)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--depth", type=int, nargs="+", default=[2, 3, 5, 10])
    parser.add_argument(
        "--followers", type=int, default=1, help="width at deepest scope"
    )
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    args = parser.parse_args()
    if any(depth < 2 for depth in args.depth) or args.followers < 1:
        parser.error("depth must be at least 2 and followers must be positive")
    try:
        scenario = yaml.safe_load(args.scenario.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        parser.error(str(error))
    states = scenario.get("states") if isinstance(scenario, dict) else None
    if not isinstance(states, dict) or any(
        not isinstance(name, str) or not isinstance(settings, dict)
        for name, settings in states.items()
    ):
        parser.error("scenario must map agent IDs to state mappings under 'states'")
    for depth in args.depth:
        scenarios(depth, args.followers - 1, states)
        print(f"PASS depth={depth} followers={args.followers}")


if __name__ == "__main__":
    main()
