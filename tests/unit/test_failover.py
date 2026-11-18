"""Deterministic failover: succession, promotion, cascade and vacancy."""

import pytest

from src.agents.hierarchy import Hierarchy, HierarchyError
from src.roles.failover import choose_successor, succession_candidates
from src.roles.manager import Role, RoleError, RoleManager, basic_eligibility
from src.schema.validator import validate

# ground commands three agents (listed out of order on purpose); agent-a has
# a follower of its own.
CHAIN = Hierarchy(
    {
        "ground": None,
        "agent-b": "ground",
        "agent-a": "ground",
        "agent-c": "ground",
        "agent-a1": "agent-a",
    }
)
SURVIVORS = ("agent-a", "agent-b", "agent-c", "agent-a1")
FIT = {"battery_percent": 90, "current_command": None}
UNFIT = {"battery_percent": 5, "current_command": None}
check = basic_eligibility()


def fleet(hierarchy=CHAIN, live=None, shared=None, eligibility=None):
    """A Role Manager per agent. ``live`` is each agent's real status now;
    ``shared`` is the status everyone last heard (from the heartbeat)."""
    live = live or {}
    managers = {
        agent: RoleManager(
            agent,
            hierarchy,
            status=lambda a=agent: live.get(a, FIT),
            eligibility=eligibility,
        )
        for agent in hierarchy.agents
    }
    for manager in managers.values():
        for agent, status in (shared or {}).items():
            manager.observe_status(agent, status)
    return managers


def deliver(managers, messages):
    for message in messages:
        assert validate(message) == (True, []), message
        managers[message["recipient_id"]].handle(message)


def lose(managers, failed, survivors):
    """Every survivor learns the commander is gone; returns announcements."""
    out = []
    for agent in survivors:
        out += managers[agent].on_commander_lost(failed)
    return out


def heartbeat(sender, recipient, epoch):
    return {
        "message_id": f"hb-{sender}-{recipient}-{epoch}",
        "sender_id": sender,
        "recipient_id": recipient,
        "timestamp": "2026-11-13T12:00:00.000000Z",
        "schema_version": 1,
        "sender_role": "commander",
        "role_epoch": epoch,
        "type": "heartbeat",
        "payload": {"status_summary": []},
    }


def command(sender, recipient, epoch):
    message = heartbeat(sender, recipient, epoch)
    message.update(type="command", payload={"command": "HOLD"})
    message["message_id"] = f"cmd-{sender}-{epoch}"
    return message


# --- the rule (060) -------------------------------------------------------------


def test_candidates_are_direct_followers_by_id_then_one_level_deeper():
    assert succession_candidates(CHAIN, "ground") == [
        "agent-a",
        "agent-b",
        "agent-c",
        "agent-a1",
    ]


@pytest.mark.parametrize(
    ("statuses", "excluded", "expected"),
    [
        ({}, (), "agent-a"),  # unknown status: benefit of the doubt
        ({"agent-a": UNFIT}, (), "agent-b"),
        ({}, ("agent-a", "agent-b"), "agent-c"),
        ({"agent-a": UNFIT, "agent-b": UNFIT, "agent-c": UNFIT}, (), "agent-a1"),
        ({a: UNFIT for a in SURVIVORS}, (), None),
    ],
    ids=["first", "skip-unfit", "skip-excluded", "deeper", "vacant"],
)
def test_choose_successor(statuses, excluded, expected):
    chosen = choose_successor(
        CHAIN, "ground", statuses=statuses, eligibility=check, excluded=excluded
    )
    assert chosen == expected


def test_succession_applies_configured_role_terrain_and_equipment():
    eligibility = basic_eligibility(
        unsuitable_terrain=("fixture",), required_capabilities=("item-a", "item-b")
    )
    statuses = {
        "agent-a": {**FIT, "terrain": "fixture", "capabilities": ["item-a", "item-b"]},
        "agent-b": {**FIT, "capabilities": ["item-a"]},
        "agent-c": {**FIT, "capabilities": ["item-b", "item-a"]},
    }

    assert (
        choose_successor(CHAIN, "ground", statuses=statuses, eligibility=eligibility)
        == "agent-c"
    )


def test_successor_takes_the_failed_place_and_keeps_its_followers():
    after = CHAIN.after_failure("ground", "agent-a", role_epoch=1)
    assert after.top == "agent-a"
    assert set(after.children_of("agent-a")) == {
        "agent-a1",
        "agent-b",
        "agent-c",
        "ground",  # rejoins as a follower if it comes back
    }
    assert after.role_epoch == 1


def test_deeper_successor_moves_up_past_its_old_commander():
    chain = Hierarchy({"top": None, "d": "top", "d1": "d", "d2": "d"})
    after = chain.after_failure("top", "d1", role_epoch=1)
    assert after.top == "d1"
    assert set(after.children_of("d1")) == {"d", "top"}
    assert after.parent_of("d2") == "d"


def test_takeover_needs_a_successor_below_and_a_newer_epoch():
    with pytest.raises(HierarchyError, match="not below"):
        CHAIN.after_failure("agent-a", "agent-b", role_epoch=1)
    with pytest.raises(HierarchyError, match="must be greater"):
        CHAIN.after_failure("ground", "agent-a", role_epoch=0)


# --- self-promotion (063) -------------------------------------------------------


def test_lowest_id_fit_agent_promotes_and_peers_accept_it():
    managers = fleet()
    announcements = lose(managers, "ground", SURVIVORS)

    # Only agent-a acted, and it told each of its new followers.
    assert {m["sender_id"] for m in announcements} == {"agent-a"}
    assert {m["recipient_id"] for m in announcements} == {
        "agent-a1",
        "agent-b",
        "agent-c",
    }
    assert all(m["role_epoch"] == 1 for m in announcements)

    deliver(managers, announcements)

    survivors = [managers[a] for a in SURVIVORS]
    assert [m.agent_id for m in survivors if m.role is Role.COMMANDER] == ["agent-a"]
    for manager in survivors:
        assert manager.hierarchy == managers["agent-a"].hierarchy
        assert manager.role_epoch == 1
        assert manager.expected_successor is None
    assert managers["agent-b"].get_current_commander() == "agent-a"
    (record,) = [r for r in managers["agent-a"].records if r["outcome"]]
    assert record["outcome"] == "self_promoted"
    assert (record["old_epoch"], record["new_epoch"]) == (0, 1)
    assert record["previous_commander_id"] == "ground"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"battery_percent": 5}, "degraded"),
        ({"mechanical_fault": True}, "degraded"),
        ({"current_command": "RETURN"}, "busy"),
        ({"terrain": "test-terrain"}, "terrain_unsuitable"),
        ({"capabilities": []}, "equipment_missing"),
    ],
)
def test_unfit_at_promotion_declines_and_the_next_takes_over(overrides, reason):
    fit = {**FIT, "capabilities": ["item-a"]}
    live = {agent: fit for agent in SURVIVORS}
    live["agent-a"] = {**fit, **overrides}
    managers = fleet(
        live=live,
        shared={agent: fit for agent in SURVIVORS},
        eligibility=basic_eligibility(
            required_capabilities=("item-a",), unsuitable_terrain=("test-terrain",)
        ),
    )

    announcements = lose(managers, "ground", SURVIVORS)

    assert announcements == []
    declined = [r for r in managers["agent-a"].records if r["outcome"]]
    assert declined[0]["outcome"] == "promotion_declined"
    assert declined[0]["reason"] == reason
    # The others wait for agent-a, time out, and move to agent-b.
    assert managers["agent-b"].expected_successor == "agent-a"
    for agent in ("agent-b", "agent-c", "agent-a1"):
        announcements += managers[agent].candidate_timed_out()
    assert {m["sender_id"] for m in announcements} == {"agent-b"}

    deliver(managers, announcements)

    assert managers["agent-b"].role is Role.COMMANDER
    for agent in ("agent-a", "agent-c"):
        assert managers[agent].get_current_commander() == "agent-b"
    assert managers["agent-a"].hierarchy == managers["agent-b"].hierarchy


def test_only_direct_follower_unfit_searches_one_level_deeper():
    chain = Hierarchy({"top": None, "d": "top", "d1": "d", "d2": "d"})
    managers = fleet(chain, shared={"d": UNFIT})

    announcements = lose(managers, "top", ("d", "d1", "d2"))
    deliver(managers, announcements)

    assert managers["d1"].role is Role.COMMANDER
    assert managers["d"].get_current_commander() == "d1"
    assert managers["d2"].get_current_commander() == "d"  # unchanged below


def test_nobody_fit_leaves_the_role_vacant_without_crashing():
    managers = fleet(shared={a: UNFIT for a in SURVIVORS})

    assert lose(managers, "ground", SURVIVORS) == []

    for agent in SURVIVORS:
        manager = managers[agent]
        assert manager.vacant
        assert manager.get_current_commander() is None
        assert manager.role_epoch == 0
        assert [r["outcome"] for r in manager.records] == ["vacant"]


def test_failover_needs_a_commander_above():
    managers = fleet()
    with pytest.raises(RoleError, match="no commander to lose"):
        managers["ground"].on_commander_lost()
    with pytest.raises(RoleError, match="not below"):
        managers["agent-b"].on_commander_lost("agent-a")


def test_survivor_that_missed_the_loss_still_accepts_the_designated_successor():
    managers = fleet()
    announcements = managers["agent-a"].on_commander_lost("ground")
    # agent-c never noticed ground was gone.
    deliver(managers, [m for m in announcements if m["recipient_id"] == "agent-c"])
    assert managers["agent-c"].get_current_commander() == "agent-a"
    assert managers["agent-c"].role_epoch == 1


# --- stale authority after failure (064) and delayed messages (073) -------------


def test_revived_commander_steps_down_and_its_old_orders_are_stale():
    managers = fleet()
    deliver(managers, lose(managers, "ground", SURVIVORS))
    ground = managers["ground"]  # was away; still thinks it is the top at 0

    # Its delayed order from before the failure reaches agent-b now.
    assert managers["agent-b"].observe(command("ground", "agent-b", 0)) is False
    stale = [r for r in managers["agent-b"].records if r.get("event_type")]
    assert stale[0]["event_type"] == "STALE_EPOCH_REJECTED"

    # Back online, it hears the new commander and rejoins as a follower.
    ground.observe(heartbeat("agent-a", "ground", 1))
    assert ground.role is Role.FOLLOWER
    assert ground.get_current_commander() == "agent-a"
    assert [r["outcome"] for r in ground.records] == ["stepped_down"]
    with pytest.raises(RoleError):
        ground.on_commander_lost("ground")


# --- out-of-turn promotion (075) ------------------------------------------------


def test_out_of_turn_promotion_is_refused():
    managers = fleet()
    lose(managers, "ground", ("agent-b",))  # agent-b expects agent-a

    accepted = managers["agent-b"].observe(heartbeat("agent-c", "agent-b", 1))

    assert accepted is False
    assert managers["agent-b"].role_epoch == 0
    assert managers["agent-b"].get_current_commander() == "ground"
    (event,) = [r for r in managers["agent-b"].records if r.get("event_type")]
    assert event["event_type"] == "UNAUTHORIZED_PROMOTION"
    assert event["detail"]["expected"] == "agent-a"


def test_out_of_turn_claim_without_any_failure_is_refused():
    managers = fleet()
    assert managers["agent-b"].observe(heartbeat("agent-c", "agent-b", 5)) is False
    assert managers["agent-b"].role_epoch == 0


# --- the commander above a failover ---------------------------------------------

# top -> mid -> {x, y}, and x has x1 below it. mid is the one that fails.
DEEP = Hierarchy({"top": None, "mid": "top", "x": "mid", "y": "mid", "x1": "x"})


def report(sender, recipient, epoch):
    message = heartbeat(sender, recipient, epoch)
    message.update(sender_role="follower", payload={"status": {"battery_percent": 80}})
    return message


def test_the_successor_reports_to_the_commander_above_which_stays_top():
    managers = fleet(DEEP)
    out = lose(managers, "mid", ("x", "y"))

    (up,) = [m for m in out if m["recipient_id"] == "top"]
    assert (up["sender_id"], up["sender_role"], up["role_epoch"]) == (
        "x",
        "follower",
        1,
    )
    deliver(managers, [up])

    top = managers["top"]
    assert top.role is Role.COMMANDER
    assert top.followers() == ("x",)
    assert top.role_epoch == 1
    (change,) = [r for r in top.records if r.get("outcome")]
    assert change["outcome"] == "successor_accepted"
    assert change["requester_id"] == "x"
    assert change["previous_commander_id"] == "mid"
    assert change["new_role"] == "commander"


def test_a_successor_from_one_level_deeper_is_accepted_above():
    unfit = {"x": UNFIT, "y": UNFIT}
    managers = fleet(DEEP, live=unfit, shared=unfit)
    out = lose(managers, "mid", ("x", "y", "x1"))

    (up,) = [m for m in out if m["recipient_id"] == "top"]
    assert up["sender_id"] == "x1"
    deliver(managers, [up])
    assert managers["top"].followers() == ("x1",)
    assert managers["top"].role is Role.COMMANDER


def test_a_newer_epoch_from_a_direct_follower_does_not_unseat_the_top():
    managers = fleet(DEEP)
    top = managers["top"]

    assert top.observe(report("mid", "top", 3)) is True

    assert top.role is Role.COMMANDER
    assert top.role_epoch == 3
    assert top.followers() == ("mid",)
    assert not [r for r in top.records if r.get("outcome")]


def test_a_successor_waiting_on_another_can_time_it_out():
    managers = fleet(DEEP)
    managers["y"].on_commander_lost("mid")
    assert managers["y"].awaited_successor == "x"

    managers["y"].candidate_timed_out()  # x never announced itself
    assert managers["y"].awaited_successor is None  # y is next, and promoted
    assert managers["y"].followers() == ("mid", "x")
