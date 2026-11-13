"""A middle agent acting as follower upward and commander downward at once."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.agents import Commander, Follower, Outcome
from src.agents.dual_role import (
    DualRoleAgent,
    DualRoleError,
    exchange_logger_for,
)
from src.agents.hierarchy import Hierarchy
from src.decision import DecisionResult
from src.schema.validator import validate
from src.state import FollowerState
from src.transport import InMemoryTransport, TransportTimeoutError

SPONSOR = Hierarchy(
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


@pytest.fixture
def chain():
    """ground (plain Commander) -> drone-cmd (DualRoleAgent) -> drone-a/b."""
    ground_end, cmd_up = pair()
    cmd_to_a, a_end = pair()
    cmd_to_b, b_end = pair()

    ground = Commander("ground")
    ground.add_follower("drone-cmd", ground_end)
    middle = DualRoleAgent(
        "drone-cmd",
        SPONSOR,
        parent_link=cmd_up,
        follower_links={"drone-a": cmd_to_a, "drone-b": cmd_to_b},
        decision_engine=comply,
    )
    drones = {
        name: Follower(
            name,
            end,
            decision_engine=comply,
            state=FollowerState(parent_id="drone-cmd", movement_flag=True),
        )
        for name, end in (("drone-a", a_end), ("drone-b", b_end))
    }
    links = {
        "ground": ground_end,
        "cmd_up": cmd_up,
        "drone-a": a_end,
        "drone-b": b_end,
    }
    middle.start()
    yield ground, middle, drones, links
    middle.stop()
    for end in (ground_end, cmd_up, cmd_to_a, a_end, cmd_to_b, b_end):
        end.close()


def envelope(sender, recipient, *, role="commander", type_="command", **payload):
    message = {
        "message_id": f"{sender}-{recipient}-1",
        "sender_id": sender,
        "recipient_id": recipient,
        "timestamp": "2026-11-13T12:00:00.000000Z",
        "schema_version": 1,
        "sender_role": role,
        "role_epoch": 0,
        "type": type_,
        "payload": payload or {"command": "HOLD"},
    }
    return message


# --- wiring ---------------------------------------------------------------------


def test_middle_agent_has_both_modules(chain):
    _, middle, _, _ = chain
    assert middle.follower is not None
    assert middle.follower.state.parent_id == "ground"
    assert middle.follower.state.follower_ids == ["drone-a", "drone-b"]
    assert middle.follower.state.hierarchy_level == 1
    assert middle.commander.followers == ["drone-a", "drone-b"]


def test_links_must_match_the_hierarchy():
    up, _ = pair()
    down, _ = pair()
    with pytest.raises(DualRoleError, match="parent link"):
        DualRoleAgent("drone-cmd", SPONSOR, follower_links={"drone-a": down})
    with pytest.raises(DualRoleError, match="don't match"):
        DualRoleAgent(
            "drone-cmd", SPONSOR, parent_link=up, follower_links={"drone-a": down}
        )
    with pytest.raises(DualRoleError, match="parent link"):
        DualRoleAgent(
            "ground", SPONSOR, parent_link=up, follower_links={"drone-cmd": down}
        )


def test_top_and_leaf_agents():
    down, _ = pair()
    top = DualRoleAgent("ground", SPONSOR, follower_links={"drone-cmd": down})
    assert top.follower is None
    assert top.commander.followers == ["drone-cmd"]

    up, _ = pair()
    leaf = DualRoleAgent("drone-a", SPONSOR, parent_link=up, decision_engine=comply)
    assert leaf.follower is not None
    assert leaf.commander.followers == []


# --- routing --------------------------------------------------------------------


def test_route_by_sender_from_the_known_peers_table(chain):
    _, middle, _, _ = chain
    assert middle.route(envelope("ground", "drone-cmd"), "ground") == "follower"
    response = envelope("drone-a", "drone-cmd", role="follower", type_="response")
    assert middle.route(response, "drone-a") == "commander"
    assert middle.route(envelope("stranger", "drone-cmd"), "ground") is None
    # The sender must match the link it came on.
    assert middle.route(envelope("ground", "drone-cmd"), "drone-a") is None
    assert middle.route({"no": "sender"}, "ground") is None


def test_both_directions_at_once(chain):
    ground, middle, drones, _ = chain
    with ThreadPoolExecutor(max_workers=4) as pool:
        # The middle agent answers its commander...
        upward = pool.submit(middle.follower.handle_once, timeout=2.0)
        from_ground = pool.submit(ground.issue, "drone-cmd", "HOLD", timeout=2.0)
        # ...while giving its own followers an order.
        handled = [
            pool.submit(drone.handle_once, timeout=2.0) for drone in drones.values()
        ]
        sent = middle.commander.broadcast("RETURN")
        downward = {
            fid: middle.commander.await_response(msg, timeout=2.0)
            for fid, msg in sent.sent.items()
        }
        upward.result(timeout=2.0)
        result_from_ground = from_ground.result(timeout=2.0)
        for future in handled:
            future.result(timeout=2.0)

    assert result_from_ground.outcome is Outcome.COMPLY
    assert {fid: r.outcome for fid, r in downward.items()} == {
        "drone-a": Outcome.COMPLY,
        "drone-b": Outcome.COMPLY,
    }
    assert all(d.state.current_command == "RETURN" for d in drones.values())
    assert not middle.rejected


def test_unknown_sender_gets_an_error_and_reaches_no_module(chain):
    _, middle, _, links = chain
    links["ground"].send(envelope("stranger", "drone-cmd"))

    reply = links["ground"].receive(timeout=2.0)

    assert reply["payload"]["response"] == "ERROR"
    assert reply["payload"]["error"]["code"] == "INVALID_SENDER_OR_RECIPIENT"
    assert reply["payload"]["in_reply_to"] == "stranger-drone-cmd-1"
    assert reply["recipient_id"] == "ground"
    assert validate(reply) == (True, [])
    with pytest.raises(TransportTimeoutError):
        middle.follower.handle_once(timeout=0.2)
    (rejected, reason) = middle.rejected[0]
    assert rejected["sender_id"] == "stranger"
    assert "not this agent's commander or follower" in reason


def test_spoofed_sender_on_the_wrong_link_is_rejected(chain):
    _, middle, _, links = chain
    # drone-a's link carries a command that claims to come from ground.
    links["drone-a"].send(envelope("ground", "drone-cmd"))

    reply = links["drone-a"].receive(timeout=2.0)

    assert reply["payload"]["error"]["code"] == "INVALID_SENDER_OR_RECIPIENT"
    assert reply["sender_role"] == "commander"  # drone-cmd commands drone-a
    with pytest.raises(TransportTimeoutError):
        middle.follower.handle_once(timeout=0.2)
    assert "came from 'drone-a'" in middle.rejected[0][1]


def test_unknown_responses_are_rejected_but_not_answered(chain):
    _, middle, _, links = chain
    stray = envelope("stranger", "drone-cmd", role="follower", type_="response")
    stray["payload"] = {"response": "COMPLY", "in_reply_to": "x"}
    links["drone-a"].send(stray)

    with pytest.raises(TransportTimeoutError):
        links["drone-a"].receive(timeout=0.3)
    assert middle.rejected[0][0]["sender_id"] == "stranger"


def test_heartbeat_from_parent_reaches_the_follower_module(chain):
    _, middle, _, links = chain
    heartbeat = envelope(
        "ground",
        "drone-cmd",
        type_="heartbeat",
        status_summary=[],
    )
    links["ground"].send(heartbeat)

    assert middle.follower.handle_once(timeout=2.0) is None  # read and ignored
    assert not middle.rejected


def test_reject_hook_is_called(chain):
    _, middle, _, links = chain
    seen = []
    middle._on_reject = lambda message, reason: seen.append(message["sender_id"])
    links["ground"].send(envelope("stranger", "drone-cmd"))
    links["ground"].receive(timeout=2.0)
    assert seen == ["stranger"]


# --- logging --------------------------------------------------------------------


def test_middle_agent_logs_levels_from_the_hierarchy(tmp_path):
    ground_end, cmd_up = pair()
    cmd_to_a, a_end = pair()
    log = exchange_logger_for(SPONSOR, "drone-cmd", tmp_path, session_id="s")
    middle = DualRoleAgent(
        "drone-cmd",
        Hierarchy({"ground": None, "drone-cmd": "ground", "drone-a": "drone-cmd"}),
        parent_link=cmd_up,
        follower_links={"drone-a": cmd_to_a},
        decision_engine=comply,
        exchange_log=log,
    )
    ground = Commander("ground")
    ground.add_follower("drone-cmd", ground_end)
    drone = Follower(
        "drone-a",
        a_end,
        decision_engine=comply,
        state=FollowerState(parent_id="drone-cmd"),
    )
    with middle, ThreadPoolExecutor(max_workers=3) as pool:
        up = pool.submit(middle.follower.handle_once, timeout=2.0)
        ground.issue("drone-cmd", "HOLD", timeout=2.0)
        up.result(timeout=2.0)
        down = pool.submit(drone.handle_once, timeout=2.0)
        middle.commander.issue("drone-a", "HOLD", timeout=2.0)
        down.result(timeout=2.0)

    records = [json.loads(line) for line in log.path.read_text().splitlines()]
    levels = {
        (r["direction"], r["message"]["sender_id"]): r["hierarchy_level"]
        for r in records
    }
    assert levels == {
        ("received", "ground"): 0,
        ("sent", "drone-cmd"): 1,
        ("received", "drone-a"): 2,
    }


# --- any depth ------------------------------------------------------------------


@pytest.mark.parametrize("depth", [2, 3, 5, 8])
def test_every_level_commands_the_next_at_once(depth):
    ids = [f"agent-{n}" for n in range(depth)]
    hierarchy = Hierarchy.chain(ids)
    ends = [pair() for _ in range(depth - 1)]  # ends[i] links ids[i] -> ids[i+1]
    agents = []
    for level, agent in enumerate(ids):
        agents.append(
            DualRoleAgent(
                agent,
                hierarchy,
                parent_link=ends[level - 1][1] if level else None,
                follower_links=(
                    {ids[level + 1]: ends[level][0]} if level < depth - 1 else {}
                ),
                decision_engine=comply,
            )
        )
    for agent in agents:
        agent.start()
    try:
        with ThreadPoolExecutor(max_workers=2 * depth) as pool:
            followers = [
                pool.submit(a.follower.handle_once, timeout=3.0) for a in agents[1:]
            ]
            orders = [
                pool.submit(a.commander.issue, ids[i + 1], "HOLD", timeout=3.0)
                for i, a in enumerate(agents[:-1])
            ]
            outcomes = [order.result(timeout=5.0).outcome for order in orders]
            for future in followers:
                future.result(timeout=5.0)
    finally:
        for agent in agents:
            agent.stop()
        for a, b in ends:
            a.close()
            b.close()

    assert outcomes == [Outcome.COMPLY] * (depth - 1)
    for agent in agents:
        assert not agent.rejected
