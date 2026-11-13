"""Role Manager: authority tracking, requests, grants, denies, epochs (045-051)."""

import pytest

from src.agents.hierarchy import Hierarchy
from src.roles.manager import (
    DenyReason,
    Role,
    RoleError,
    RoleManager,
    basic_eligibility,
)
from src.schema.validator import validate

SPONSOR = Hierarchy(
    {
        "ground": None,
        "drone-cmd": "ground",
        "drone-a": "drone-cmd",
        "drone-b": "drone-cmd",
        "drone-c": "drone-cmd",
    }
)
FIT = {"battery_percent": 90, "current_command": None, "capabilities": []}


def managers(hierarchy=SPONSOR, statuses=None, **kwargs):
    statuses = statuses or {}
    return {
        agent: RoleManager(
            agent,
            hierarchy,
            status=lambda agent=agent: statuses.get(agent, FIT),
            **kwargs,
        )
        for agent in hierarchy.agents
    }


def deliver(fleet, *messages):
    """Hand each message to its recipient and keep going with the replies."""
    delivered = []
    queue = list(messages)
    while queue:
        message = queue.pop(0)
        assert validate(message) == (True, []), message
        delivered.append(message)
        queue.extend(fleet[message["recipient_id"]].handle(message))
    return delivered


def commanders(fleet):
    return sorted(a for a, m in fleet.items() if m.role is Role.COMMANDER)


# --- 045: the Role Manager tracks authority -------------------------------------


def test_roles_and_peers_follow_the_hierarchy():
    fleet = managers()
    assert fleet["ground"].role is Role.COMMANDER
    assert fleet["ground"].get_current_commander() is None
    assert fleet["drone-cmd"].role is Role.FOLLOWER
    assert fleet["drone-cmd"].get_current_commander() == "ground"
    # A middle agent is a follower upward and still commands downward.
    assert fleet["drone-cmd"].followers() == ("drone-a", "drone-b", "drone-c")
    assert fleet["drone-cmd"].known_peers() == {
        "ground": "commander",
        "drone-a": "follower",
        "drone-b": "follower",
        "drone-c": "follower",
    }
    assert fleet["drone-a"].known_peers() == {"drone-cmd": "commander"}
    assert commanders(fleet) == ["ground"]


def test_agent_must_be_in_the_hierarchy():
    with pytest.raises(RoleError, match="not in the hierarchy"):
        RoleManager("stranger", SPONSOR)


def test_waiting_for_an_answer_is_transitioning():
    fleet = managers()
    fleet["drone-a"].request_role()
    assert fleet["drone-a"].role is Role.TRANSITIONING
    fleet["drone-a"].cancel_request()
    assert fleet["drone-a"].role is Role.FOLLOWER


# --- 046 / 047 / 048: request and grant (incl. skip-level) ----------------------


def test_skip_level_request_straight_to_the_top_is_granted():
    fleet = managers()
    request = fleet["drone-a"].request_role()  # defaults to the top
    assert request["recipient_id"] == "ground"
    assert request["payload"] == {"target_id": "ground", "requested_role": "commander"}

    sent = deliver(fleet, request)

    assert [m["type"] for m in sent] == ["role_request", "role_grant"]
    assert sent[1]["payload"]["new_epoch"] == 1
    a, ground = fleet["drone-a"], fleet["ground"]
    # Both managers agree on the new chain and the new epoch.
    assert a.hierarchy == ground.hierarchy
    assert a.role_epoch == ground.role_epoch == 1
    assert a.role is Role.COMMANDER
    assert ground.role is Role.FOLLOWER
    assert ground.get_current_commander() == "drone-a"
    # The previous hierarchy nests one level deeper; lower levels stay intact.
    chain = a.hierarchy
    assert chain.level_of("ground") == 1
    assert chain.level_of("drone-cmd") == 2
    assert chain.parent_of("drone-b") == "drone-cmd"
    assert chain.level_of("drone-b") == 3


def test_middle_agent_keeps_its_followers_when_granted():
    fleet = managers()
    deliver(fleet, fleet["drone-cmd"].request_role("ground"))
    chain = fleet["drone-cmd"].hierarchy
    assert chain.top == "drone-cmd"
    assert set(chain.children_of("drone-cmd")) == {
        "drone-a",
        "drone-b",
        "drone-c",
        "ground",
    }


def test_epoch_increments_only_on_grant():
    fleet = managers(statuses={"drone-b": {**FIT, "battery_percent": 50}})
    fleet["ground"].observe_status("drone-a", {**FIT, "mechanical_fault": True})

    deliver(fleet, fleet["drone-a"].request_role())  # denied: degraded
    assert fleet["ground"].role_epoch == 0
    assert fleet["drone-a"].role_epoch == 0

    deliver(fleet, fleet["drone-b"].request_role())  # granted
    assert fleet["ground"].role_epoch == fleet["drone-b"].role_epoch == 1


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ({**FIT, "battery_percent": 10}, "degraded"),
        ({**FIT, "mechanical_fault": True}, "degraded"),
        ({**FIT, "current_command": "RETURN"}, "busy"),
        ({**FIT, "terrain": "swamp"}, "terrain_unsuitable"),
        ({**FIT, "capabilities": []}, "equipment_missing"),
    ],
)
def test_grant_checks_the_requesters_eligibility(status, reason):
    check = basic_eligibility(
        required_capabilities=("radio",), unsuitable_terrain=("swamp",)
    )
    fleet = managers(
        eligibility=check, statuses={"drone-a": {**FIT, "capabilities": ["radio"]}}
    )
    fleet["ground"].observe_status("drone-a", status)

    sent = deliver(fleet, fleet["drone-a"].request_role())

    assert sent[-1]["type"] == "role_deny"
    assert sent[-1]["payload"]["reason"] == reason
    assert fleet["drone-a"].last_deny == reason
    assert fleet["drone-a"].role is Role.FOLLOWER
    assert commanders(fleet) == ["ground"]


def test_requester_checks_itself_before_asking():
    fleet = managers(statuses={"drone-a": {**FIT, "battery_percent": 5}})
    with pytest.raises(RoleError, match="eligibility check: degraded"):
        fleet["drone-a"].request_role()
    assert fleet["drone-a"].role is Role.FOLLOWER


@pytest.mark.parametrize(
    ("requester", "target", "message"),
    [
        ("drone-a", "drone-b", "not a commander above"),
        ("drone-a", "drone-a", "not a commander above"),
        ("ground", None, "already the commander"),
    ],
)
def test_nonsense_requests_are_refused_locally(requester, target, message):
    fleet = managers()
    with pytest.raises(RoleError, match=message):
        fleet[requester].request_role(target)


def test_one_request_at_a_time():
    fleet = managers()
    fleet["drone-a"].request_role()
    with pytest.raises(RoleError, match="already waiting"):
        fleet["drone-a"].request_role()


# --- 049: deny with one documented reason ---------------------------------------


def test_request_to_an_in_between_commander_is_not_authorized():
    fleet = managers()
    sent = deliver(fleet, fleet["drone-a"].request_role("drone-cmd"))
    assert sent[-1]["type"] == "role_deny"
    assert sent[-1]["payload"]["reason"] == DenyReason.NOT_AUTHORIZED.value
    assert fleet["drone-cmd"].role_epoch == 0


def test_misaddressed_request_is_not_authorized():
    fleet = managers()
    request = fleet["drone-a"].request_role()
    request["payload"]["target_id"] = "drone-cmd"  # says one thing, sent to another
    sent = deliver(fleet, request)
    assert sent[-1]["payload"]["reason"] == "not_authorized"


def test_deny_reasons_match_the_schema():
    import json
    from pathlib import Path

    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "src/schema/falcon-v1.json").read_text()
    )
    assert sorted(r.value for r in DenyReason) == sorted(
        schema["$defs"]["deny_reason"]["enum"]
    )


# --- 050: exactly one commander, one-level propagation --------------------------


def test_concurrent_requests_give_exactly_one_commander():
    fleet = managers()
    first = fleet["drone-a"].request_role()
    second = fleet["drone-b"].request_role()

    sent = deliver(fleet, first, second)

    grants = [m for m in sent if m["type"] == "role_grant"]
    assert [g["recipient_id"] for g in grants] == ["drone-a"]
    stale = [m for m in sent if m["type"] == "response"]
    assert stale[0]["recipient_id"] == "drone-b"
    assert stale[0]["payload"]["error"]["code"] == "STALE_EPOCH"
    assert fleet["drone-b"].role is Role.FOLLOWER  # its request is over
    # Among the agents that know about the handover, one commander.
    assert commanders({k: fleet[k] for k in ("ground", "drone-a", "drone-b")}) == [
        "drone-a"
    ]


def test_only_the_level_below_is_notified():
    fleet = managers()
    deliver(fleet, fleet["drone-a"].request_role())
    # The new top and the old top (now one level below it) updated; deeper
    # agents were sent nothing and keep their view.
    assert fleet["drone-a"].role_epoch == fleet["ground"].role_epoch == 1
    for agent in ("drone-cmd", "drone-b", "drone-c"):
        assert fleet[agent].role_epoch == 0
        assert fleet[agent].hierarchy == SPONSOR


def test_repeated_request_after_grant_is_refused():
    fleet = managers()
    deliver(fleet, fleet["drone-a"].request_role())
    with pytest.raises(RoleError, match="already the commander"):
        fleet["drone-a"].request_role()


def test_duplicate_request_message_is_stale_the_second_time():
    fleet = managers()
    request = fleet["drone-a"].request_role()
    deliver(fleet, request)
    replies = fleet["ground"].handle(request)
    assert replies[0]["payload"]["error"]["code"] == "STALE_EPOCH"
    assert fleet["ground"].role_epoch == 1


# --- 051: stale epochs and the revived commander --------------------------------


def command(sender, recipient, epoch, role="commander"):
    return {
        "message_id": f"{sender}-{epoch}",
        "sender_id": sender,
        "recipient_id": recipient,
        "timestamp": "2026-11-13T12:00:00.000000Z",
        "schema_version": 1,
        "sender_role": role,
        "role_epoch": epoch,
        "type": "command",
        "payload": {"command": "HOLD"},
    }


def test_stale_messages_are_rejected_and_logged():
    fleet = managers()
    deliver(fleet, fleet["drone-a"].request_role())
    ground = fleet["ground"]

    assert ground.observe(command("drone-a", "ground", 0)) is False

    (event,) = [r for r in ground.records if r["record_type"] == "event"]
    assert event["event_type"] == "STALE_EPOCH_REJECTED"
    assert event["severity"] == "warning"
    assert event["detail"]["epoch"] == 0
    assert event["detail"]["current_epoch"] == 1


def test_revived_commander_steps_down_and_never_reclaims():
    # ground went offline; drone-cmd took over at epoch 1 while it was away.
    fleet = managers()
    ground = fleet["ground"]
    assert ground.role is Role.COMMANDER

    ground.observe(command("drone-cmd", "ground", 1))

    assert ground.role is Role.FOLLOWER
    assert ground.role_epoch == 1
    assert ground.get_current_commander() == "drone-cmd"
    assert ground.hierarchy.top == "drone-cmd"
    (change,) = [r for r in ground.records if r["outcome"] == "stepped_down"]
    assert (change["old_epoch"], change["new_epoch"]) == (0, 1)
    # Its own old-epoch commands are now stale everywhere that knows epoch 1.
    assert fleet["drone-cmd"].observe(command("ground", "drone-cmd", 0)) is True
    fleet["drone-cmd"].observe(command("x", "drone-cmd", 1, role="follower"))
    assert fleet["drone-cmd"].observe(command("ground", "drone-cmd", 0)) is False


def test_revived_commander_waits_when_it_does_not_know_the_new_commander():
    fleet = managers()
    ground = fleet["ground"]
    stale_reply = command("drone-cmd", "ground", 1, role="follower")
    stale_reply["type"] = "response"
    stale_reply["payload"] = {
        "response": "ERROR",
        "in_reply_to": "x",
        "error": {"code": "STALE_EPOCH"},
    }

    ground.observe(stale_reply)

    assert ground.role is Role.FOLLOWER
    assert ground.get_current_commander() is None
    with pytest.raises(RoleError, match="doesn't know who commands"):
        ground.request_role()
    # Once the new commander commands it, it knows where it stands.
    ground.observe(command("drone-cmd", "ground", 1))
    assert ground.get_current_commander() == "drone-cmd"
    assert ground.role is Role.FOLLOWER


def test_middle_agent_only_adopts_a_newer_epoch():
    fleet = managers()
    middle = fleet["drone-cmd"]
    middle.observe(command("ground", "drone-cmd", 3))
    assert middle.role_epoch == 3
    assert middle.get_current_commander() == "ground"
    assert middle.hierarchy.top == "ground"
    assert not [r for r in middle.records if r["record_type"] == "role_change"]


# --- answers that don't match ---------------------------------------------------


def test_late_or_unknown_answers_are_ignored():
    fleet = managers()
    request = fleet["drone-a"].request_role()
    fleet["drone-a"].cancel_request()
    grant = fleet["ground"].handle(request)[0]

    fleet["drone-a"].handle(grant)

    assert fleet["drone-a"].role is Role.FOLLOWER
    assert fleet["drone-a"].ignored[0][1] == "no matching role request"


def test_answer_from_the_wrong_agent_is_ignored():
    fleet = managers()
    request = fleet["drone-a"].request_role()
    grant = fleet["ground"].handle(request)[0]
    grant["sender_id"] = "drone-cmd"

    fleet["drone-a"].handle(grant)

    assert fleet["drone-a"].role is Role.TRANSITIONING


def test_invalid_role_messages_are_ignored():
    fleet = managers()
    request = fleet["drone-a"].request_role()
    request["payload"]["requested_role"] = "king"
    assert fleet["ground"].handle(request) == []
    assert fleet["ground"].ignored
    assert fleet["ground"].role_epoch == 0


# --- records in the exchange-log format -----------------------------------------


def test_role_change_records_use_the_log_format():
    seen = []
    fleet = managers(on_record=seen.append, session_id="run-1")
    deliver(fleet, fleet["drone-a"].request_role())

    granted = [r for r in seen if r["outcome"] == "granted"]
    assert {r["logged_by"] for r in granted} == {"ground", "drone-a"}
    for record in granted:
        assert record["record_type"] == "role_change"
        assert record["session_id"] == "run-1"
        assert record["requester_id"] == "drone-a"
        assert record["target_commander_id"] == "ground"
        assert record["previous_commander_id"] == "ground"
        assert (record["old_epoch"], record["new_epoch"]) == (0, 1)
        assert record["new_role"] == "COMMANDER"
        assert isinstance(record["hierarchy_level"], int)
