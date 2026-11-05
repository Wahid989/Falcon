"""Commander-to-Follower round trips for every decline reason and command.

Both sides use the project validator. The decision callbacks are fixed
stand-ins that decline with a given reason; they exercise how a decline
travels between the agents, not the decision policy itself.
"""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.agents import Commander, Follower, Outcome
from src.decision import DecisionResult, DeclineReason
from src.schema.validator import validate
from src.state import FollowerState
from src.transport import InMemoryTransport

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "src/schema/falcon-v1.json").read_text(
        encoding="utf-8"
    )
)
SCHEMA_DECLINE_REASONS = SCHEMA["$defs"]["decline_reason"]["enum"]
SCHEMA_ERROR_CODES = SCHEMA["$defs"]["error_code"]["enum"]

COMMANDER_ID = "commander-1"
COMMANDS = [
    ("HOLD", None),
    ("PROCEED_TO_WAYPOINT", "north-fence"),
    ("RETURN", None),
    ("REPORT_STATUS", None),
]


def busy_state():
    """A follower mid-task, so any state change would show up."""
    return FollowerState(
        parent_id=COMMANDER_ID,
        battery_percent=40,
        obstacle_detected=True,
        mechanical_fault=False,
        terrain="rocky",
        capabilities=["camera"],
        position="east-ridge",
        movement_flag=True,
        current_command="RETURN",
        role_epoch=2,
    )


def always(decision):
    calls = []

    def engine(state, command, payload, epoch):
        calls.append((command, dict(payload), epoch))
        return decision

    engine.calls = calls
    return engine


class Link:
    """One Commander and one Follower over a paired in-memory transport."""

    def __init__(self, follower_id="follower-1", *, state=None, engine=None):
        self.commander_end, self.follower_end = InMemoryTransport.pair()
        self.commander_end.connect()
        self.follower_end.connect()
        self.follower = Follower(
            agent_id=follower_id,
            transport=self.follower_end,
            decision_engine=engine or always(DecisionResult(True)),
            state=state if state is not None else busy_state(),
        )

    def close(self):
        self.commander_end.close()
        self.follower_end.close()


@pytest.fixture
def make_link():
    links = []

    def factory(*args, **kwargs):
        link = Link(*args, **kwargs)
        links.append(link)
        return link

    yield factory
    for link in links:
        link.close()


def exchange(commander, link, follower_id, command, waypoint=None):
    """Send one command and let the follower handle it; return both results."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        handled = pool.submit(link.follower.handle_once, timeout=2.0)
        result = commander.issue(follower_id, command, waypoint=waypoint, timeout=2.0)
        return result, handled.result(timeout=2.0)


def new_commander(role_epoch=2, agent_id=COMMANDER_ID):
    return Commander(agent_id, role_epoch=role_epoch)


# --- coverage guard -------------------------------------------------------------


def test_every_schema_decline_reason_is_covered():
    # If a reason is added to the vocabulary, these tests pick it up; this
    # check fails if the code and the schema drift apart.
    assert sorted(r.value for r in DeclineReason) == sorted(SCHEMA_DECLINE_REASONS)


# --- every reason x every command -----------------------------------------------


@pytest.mark.parametrize("reason", list(DeclineReason), ids=lambda r: r.value)
@pytest.mark.parametrize(
    ("command", "waypoint"), COMMANDS, ids=[c for c, _ in COMMANDS]
)
def test_decline_round_trip(make_link, reason, command, waypoint):
    decision = DecisionResult(False, reason)
    engine = always(decision)
    link = make_link(engine=engine)
    before = copy.deepcopy(link.follower.state)
    commander = new_commander()
    commander.add_follower("follower-1", link.commander_end)

    result, handled = exchange(commander, link, "follower-1", command, waypoint)

    # The commander sees exactly the reason the follower chose.
    assert result.outcome is Outcome.DECLINE
    assert result.reason == reason.value
    assert result.response["payload"]["in_reply_to"] == result.command["message_id"]
    assert result.response["sender_id"] == "follower-1"
    assert result.response["recipient_id"] == COMMANDER_ID
    assert "state" not in result.response["payload"]
    assert validate(result.response) == (True, [])
    assert commander.rejected_responses("follower-1") == []

    # The follower decided on the command that was sent, and nothing changed.
    expected_payload = {"waypoint": waypoint} if waypoint else {}
    assert engine.calls == [(command, expected_payload, 2)]
    assert handled.decision == decision
    assert link.follower.state == before


# --- several followers, different reasons ---------------------------------------


def test_broadcast_keeps_each_followers_reason(make_link):
    reasons = {
        "follower-1": DeclineReason.OBSTACLE_DETECTED,
        "follower-2": DeclineReason.MECHANICAL_FAULT,
        "follower-3": DeclineReason.TERRAIN_UNSUITABLE,
    }
    links = {
        fid: make_link(fid, engine=always(DecisionResult(False, reason)))
        for fid, reason in reasons.items()
    }
    before = {fid: copy.deepcopy(link.follower.state) for fid, link in links.items()}
    commander = new_commander()
    for fid, link in links.items():
        commander.add_follower(fid, link.commander_end)

    sent = commander.broadcast("RETURN")
    assert sent.failed == {}
    # Answer in reverse order to show replies are matched by id, not arrival.
    for fid in reversed(list(links)):
        links[fid].follower.handle_once(timeout=1.0)
    results = {
        fid: commander.await_response(message, timeout=1.0)
        for fid, message in sent.sent.items()
    }

    for fid, reason in reasons.items():
        assert results[fid].outcome is Outcome.DECLINE
        assert results[fid].reason == reason.value
        assert links[fid].follower.state == before[fid]


def test_mixed_broadcast_complies_and_declines(make_link):
    complier = make_link("follower-1", engine=always(DecisionResult(True)))
    decliner = make_link(
        "follower-2", engine=always(DecisionResult(False, DeclineReason.BUSY))
    )
    decliner_before = copy.deepcopy(decliner.follower.state)
    commander = new_commander()
    commander.add_follower("follower-1", complier.commander_end)
    commander.add_follower("follower-2", decliner.commander_end)

    sent = commander.broadcast("HOLD")
    complier.follower.handle_once(timeout=1.0)
    decliner.follower.handle_once(timeout=1.0)
    first = commander.await_response(sent.sent["follower-1"], timeout=1.0)
    second = commander.await_response(sent.sent["follower-2"], timeout=1.0)

    assert first.outcome is Outcome.COMPLY
    assert complier.follower.state.movement_flag is False
    assert second.outcome is Outcome.DECLINE
    assert second.reason == "BUSY"
    assert decliner.follower.state == decliner_before


# --- a decline does not wedge the follower --------------------------------------


def test_follower_complies_after_a_decline(make_link):
    decisions = iter(
        [DecisionResult(False, DeclineReason.BATTERY_CRITICAL), DecisionResult(True)]
    )
    link = make_link(engine=lambda *_: next(decisions))
    before = copy.deepcopy(link.follower.state)
    commander = new_commander()
    commander.add_follower("follower-1", link.commander_end)

    declined, _ = exchange(commander, link, "follower-1", "HOLD")
    assert declined.outcome is Outcome.DECLINE
    assert link.follower.state == before

    complied, _ = exchange(commander, link, "follower-1", "HOLD")
    assert complied.outcome is Outcome.COMPLY
    assert link.follower.state.movement_flag is False
    assert link.follower.state.current_command is None


# --- protocol errors (not declines) ---------------------------------------------


def test_stale_epoch_command_gets_an_error_not_a_decision(make_link):
    engine = always(DecisionResult(True))
    link = make_link(engine=engine)  # follower is at epoch 2
    before = copy.deepcopy(link.follower.state)
    commander = new_commander(role_epoch=1)
    commander.add_follower("follower-1", link.commander_end)

    result, _ = exchange(commander, link, "follower-1", "RETURN")

    assert "STALE_EPOCH" in SCHEMA_ERROR_CODES
    assert result.outcome is Outcome.ERROR
    assert result.error["code"] == "STALE_EPOCH"
    assert validate(result.response) == (True, [])
    assert engine.calls == []  # never reached the decision step
    assert link.follower.state == before


def test_command_from_a_different_commander_is_refused(make_link):
    engine = always(DecisionResult(True))
    link = make_link(engine=engine)  # follower's parent is commander-1
    before = copy.deepcopy(link.follower.state)
    impostor = new_commander(agent_id="commander-9")
    impostor.add_follower("follower-1", link.commander_end)

    result, _ = exchange(impostor, link, "follower-1", "RETURN")

    assert "UNAUTHORIZED" in SCHEMA_ERROR_CODES
    assert result.outcome is Outcome.ERROR
    assert result.error["code"] == "UNAUTHORIZED"
    assert engine.calls == []
    assert link.follower.state == before
