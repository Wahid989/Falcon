"""Commander module: building, validating and sending commands, reading responses.

Messages are checked with the project validator (src.schema.validator) when it
exists, and otherwise directly against src/schema/falcon-v1.json, so these
tests always hold the Commander to the real protocol schema.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from src.agents.commander import (
    COMMANDS,
    KEEP_PER_FOLLOWER,
    Commander,
    CommanderError,
    InvalidCommandError,
    Outcome,
    UnknownFollowerError,
    default_validator,
)
from src.transport import InMemoryTransport, TransportClosedError, TransportTimeoutError

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "src" / "schema" / "falcon-v1.json"


def _schema_validator():
    with SCHEMA_PATH.open(encoding="utf-8") as handle:
        checker = Draft202012Validator(
            json.load(handle), format_checker=FormatChecker()
        )

    def validate(message):
        errors = sorted(checker.iter_errors(message), key=str)
        return not errors, [e.message for e in errors]

    return validate


try:
    VALIDATE = default_validator()
except CommanderError:
    VALIDATE = _schema_validator()

SCHEMA_CHECK = _schema_validator()

FOLLOWERS = ("drone-a", "drone-b", "drone-c")
STATUS = {"agent_id": "drone-a", "battery_percent": 80, "current_role": "follower"}


class Follower:
    """The follower end of one link, scripted by each test."""

    def __init__(self, agent_id, link):
        self.agent_id = agent_id
        self.link = link
        self._ids = count(1)

    def receive(self):
        return self.link.receive(timeout=1.0)

    def response(self, command, response, **payload):
        return {
            "message_id": f"{self.agent_id}-r{next(self._ids)}",
            "sender_id": self.agent_id,
            "recipient_id": command["sender_id"],
            "timestamp": "2026-11-04T12:00:01Z",
            "schema_version": 1,
            "sender_role": "follower",
            "role_epoch": command["role_epoch"],
            "type": "response",
            "payload": {
                "response": response,
                "in_reply_to": command["message_id"],
                **payload,
            },
        }

    def reply(self, command, response, **payload):
        message = self.response(command, response, **payload)
        self.link.send(message)
        return message


@pytest.fixture
def links():
    """A commander with three followers on in-memory links."""
    commander = Commander("ground", validator=VALIDATE)
    followers = {}
    opened = []
    for follower_id in FOLLOWERS:
        commander_end, follower_end = InMemoryTransport.pair()
        commander_end.connect()
        follower_end.connect()
        opened += [commander_end, follower_end]
        commander.add_follower(follower_id, commander_end)
        followers[follower_id] = Follower(follower_id, follower_end)
    yield commander, followers
    for link in opened:
        link.close()


# --- building commands --------------------------------------------------------


@pytest.mark.parametrize("command", [c for c in COMMANDS if c != "PROCEED_TO_WAYPOINT"])
def test_builds_a_valid_command(command):
    message = Commander("ground", validator=VALIDATE).build_command("drone-a", command)

    assert SCHEMA_CHECK(message) == (True, [])
    assert message["type"] == "command"
    assert message["payload"] == {"command": command}
    assert message["sender_id"] == "ground"
    assert message["recipient_id"] == "drone-a"
    assert message["sender_role"] == "commander"
    assert message["schema_version"] == 1


def test_builds_proceed_to_waypoint_with_its_waypoint():
    message = Commander("ground", validator=VALIDATE).build_command(
        "drone-a", "PROCEED_TO_WAYPOINT", waypoint="north-fence"
    )

    assert SCHEMA_CHECK(message) == (True, [])
    assert message["payload"] == {
        "command": "PROCEED_TO_WAYPOINT",
        "waypoint": "north-fence",
    }


def test_every_command_gets_a_new_message_id():
    commander = Commander("ground", validator=VALIDATE)

    ids = {commander.build_command("drone-a", "HOLD")["message_id"] for _ in range(100)}

    assert len(ids) == 100


def test_timestamp_is_utc_iso_8601():
    message = Commander("ground", validator=VALIDATE).build_command("drone-a", "HOLD")

    stamp = message["timestamp"]
    assert stamp.endswith("Z")
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    assert parsed.utcoffset() == timedelta(0)
    assert abs(datetime.now(timezone.utc) - parsed) < timedelta(seconds=5)


def test_command_carries_the_current_role_epoch():
    commander = Commander("ground", role_epoch=3, validator=VALIDATE)
    assert commander.build_command("drone-a", "HOLD")["role_epoch"] == 3

    commander.role_epoch = 4
    assert commander.build_command("drone-a", "HOLD")["role_epoch"] == 4


def test_hierarchy_level_is_never_put_on_the_wire():
    message = Commander("ground", validator=VALIDATE).build_command("drone-a", "HOLD")

    assert "hierarchy_level" not in message
    assert "hierarchy_level" not in message["payload"]


def test_clock_and_ids_can_be_injected():
    ids = iter(["m-1", "m-2"])
    commander = Commander(
        "ground",
        validator=VALIDATE,
        clock=lambda: "2026-11-04T12:00:00Z",
        new_id=lambda: next(ids),
    )

    first = commander.build_command("drone-a", "HOLD")
    second = commander.build_command("drone-a", "RETURN")

    assert (first["message_id"], first["timestamp"]) == ("m-1", "2026-11-04T12:00:00Z")
    assert second["message_id"] == "m-2"


@pytest.mark.parametrize(
    "command, waypoint",
    [
        ("PROCEED_TO_WAYPOINT", None),
        ("HOLD", "north-fence"),
        ("RETURN", "north-fence"),
        ("REPORT_STATUS", "north-fence"),
        ("PROCEED_TO_WAYPOINT", ""),
        ("LAND", None),
        ("hold", None),
        ("", None),
    ],
)
def test_invalid_commands_are_rejected(command, waypoint):
    commander = Commander("ground", validator=VALIDATE)

    with pytest.raises(InvalidCommandError) as raised:
        commander.build_command("drone-a", command, waypoint=waypoint)

    assert raised.value.errors


@pytest.mark.parametrize("agent_id", ["", "has space", "slash/id"])
def test_invalid_agent_ids_are_rejected(agent_id):
    with pytest.raises(InvalidCommandError):
        Commander(agent_id, validator=VALIDATE).build_command("drone-a", "HOLD")
    with pytest.raises(InvalidCommandError):
        Commander("ground", validator=VALIDATE).build_command(agent_id, "HOLD")


def test_negative_role_epoch_is_rejected():
    with pytest.raises(InvalidCommandError):
        Commander("ground", role_epoch=-1, validator=VALIDATE).build_command(
            "drone-a", "HOLD"
        )


def test_an_invalid_command_is_never_sent(links):
    commander, followers = links

    with pytest.raises(InvalidCommandError):
        commander.send_command("drone-a", "PROCEED_TO_WAYPOINT")

    with pytest.raises(TransportTimeoutError):
        followers["drone-a"].link.receive(timeout=0)


def test_the_validator_decides():
    calls = []

    def refuse_everything(message):
        calls.append(message)
        return False, ["refused"]

    with pytest.raises(InvalidCommandError, match="refused"):
        Commander("ground", validator=refuse_everything).build_command(
            "drone-a", "HOLD"
        )
    assert calls


# --- sending and reading responses --------------------------------------------


def test_round_trip_comply(links):
    commander, followers = links

    sent = commander.send_command("drone-a", "HOLD")
    received = followers["drone-a"].receive()
    assert received == sent

    followers["drone-a"].reply(received, "COMPLY")
    result = commander.await_response(sent, timeout=1.0)

    assert result.outcome is Outcome.COMPLY
    assert result.command == sent
    assert result.response["payload"]["in_reply_to"] == sent["message_id"]


def test_decline_carries_its_reason(links):
    commander, followers = links
    sent = commander.send_command("drone-a", "PROCEED_TO_WAYPOINT", waypoint="ridge")

    followers["drone-a"].reply(
        followers["drone-a"].receive(), "DECLINE", reason="BATTERY_CRITICAL"
    )
    result = commander.await_response(sent, timeout=1.0)

    assert result.outcome is Outcome.DECLINE
    assert result.reason == "BATTERY_CRITICAL"


def test_status_response_carries_the_state(links):
    commander, followers = links
    sent = commander.send_command("drone-a", "REPORT_STATUS")

    followers["drone-a"].reply(
        followers["drone-a"].receive(), "STATUS_RESPONSE", state=STATUS
    )
    result = commander.await_response(sent, timeout=1.0)

    assert result.outcome is Outcome.STATUS_RESPONSE
    assert result.state == STATUS


def test_error_response_carries_the_error(links):
    commander, followers = links
    sent = commander.send_command("drone-a", "HOLD")

    followers["drone-a"].reply(
        followers["drone-a"].receive(), "ERROR", error={"code": "STALE_EPOCH"}
    )
    result = commander.await_response(sent, timeout=1.0)

    assert result.outcome is Outcome.ERROR
    assert result.error == {"code": "STALE_EPOCH"}


def test_issue_sends_and_waits(links):
    commander, followers = links

    with ThreadPoolExecutor(max_workers=1) as pool:
        follower = followers["drone-a"]
        pool.submit(lambda: follower.reply(follower.receive(), "COMPLY"))
        result = commander.issue("drone-a", "RETURN", timeout=1.0)

    assert result.outcome is Outcome.COMPLY
    assert result.command["payload"] == {"command": "RETURN"}


def test_no_response_is_a_timeout_not_a_hang(links):
    commander, _ = links
    sent = commander.send_command("drone-a", "HOLD")

    result = commander.await_response(sent, timeout=0.05)

    assert result.outcome is Outcome.TIMEOUT
    assert result.response is None


def test_responses_are_matched_by_in_reply_to(links):
    commander, followers = links
    first = commander.send_command("drone-a", "HOLD")
    second = commander.send_command("drone-a", "RETURN")
    follower = followers["drone-a"]
    one, two = follower.receive(), follower.receive()

    # The follower answers the second command first.
    follower.reply(two, "DECLINE", reason="BUSY")
    follower.reply(one, "COMPLY")

    assert commander.await_response(first, timeout=1.0).outcome is Outcome.COMPLY
    second_result = commander.await_response(second, timeout=1.0)
    assert second_result.outcome is Outcome.DECLINE
    assert second_result.reason == "BUSY"


def test_non_response_messages_are_kept_for_later(links):
    commander, followers = links
    sent = commander.send_command("drone-a", "HOLD")
    follower = followers["drone-a"]
    command = follower.receive()
    heartbeat = {
        **follower.response(command, "COMPLY"),
        "type": "heartbeat",
        "payload": {"status": {"battery_percent": 80}},
    }
    follower.link.send(heartbeat)
    follower.reply(command, "COMPLY")

    assert commander.await_response(sent, timeout=1.0).outcome is Outcome.COMPLY
    assert commander.other_messages("drone-a") == [heartbeat]
    assert commander.other_messages("drone-a") == []


def test_an_invalid_response_to_the_command_is_reported(links):
    commander, followers = links
    sent = commander.send_command("drone-a", "HOLD")
    follower = followers["drone-a"]
    bad = follower.response(follower.receive(), "DECLINE")  # DECLINE needs a reason
    follower.link.send(bad)

    result = commander.await_response(sent, timeout=1.0)

    assert result.outcome is Outcome.INVALID_RESPONSE
    assert result.response == bad
    assert result.problems
    assert commander.rejected_responses("drone-a")[0][0] == bad


def test_a_response_from_the_wrong_sender_is_rejected(links):
    commander, followers = links
    sent = commander.send_command("drone-a", "HOLD")
    follower = followers["drone-a"]
    forged = {**follower.response(follower.receive(), "COMPLY"), "sender_id": "drone-b"}
    follower.link.send(forged)

    result = commander.await_response(sent, timeout=1.0)

    assert result.outcome is Outcome.INVALID_RESPONSE
    assert any("sender_id" in problem for problem in result.problems)


def test_a_response_addressed_to_someone_else_is_rejected(links):
    commander, followers = links
    sent = commander.send_command("drone-a", "HOLD")
    follower = followers["drone-a"]
    misrouted = {
        **follower.response(follower.receive(), "COMPLY"),
        "recipient_id": "other-cmd",
    }
    follower.link.send(misrouted)

    result = commander.await_response(sent, timeout=1.0)

    assert result.outcome is Outcome.INVALID_RESPONSE
    assert any("recipient_id" in problem for problem in result.problems)


def test_garbage_that_answers_nothing_does_not_end_the_wait(links):
    commander, followers = links
    sent = commander.send_command("drone-a", "HOLD")
    follower = followers["drone-a"]
    command = follower.receive()
    follower.link.send({"type": "response", "payload": "nonsense"})
    follower.reply(command, "COMPLY")

    result = commander.await_response(sent, timeout=1.0)

    assert result.outcome is Outcome.COMPLY
    assert len(commander.rejected_responses("drone-a")) == 1


def test_broadcast_reaches_every_follower_once(links):
    commander, followers = links

    result = commander.broadcast("HOLD")
    sent = result.sent

    assert result.failed == {}
    assert set(sent) == set(FOLLOWERS)
    assert len({m["message_id"] for m in sent.values()}) == len(FOLLOWERS)
    for follower_id, follower in followers.items():
        received = follower.receive()
        assert received == sent[follower_id]
        assert received["recipient_id"] == follower_id
        reason = {"reason": "BATTERY_CRITICAL"} if follower_id == "drone-c" else {}
        follower.reply(received, "DECLINE" if reason else "COMPLY", **reason)

    outcomes = {
        follower_id: commander.await_response(message, timeout=1.0).outcome
        for follower_id, message in sent.items()
    }
    assert outcomes == {
        "drone-a": Outcome.COMPLY,
        "drone-b": Outcome.COMPLY,
        "drone-c": Outcome.DECLINE,
    }


# --- followers and links ------------------------------------------------------


def test_unknown_follower_is_a_clear_error(links):
    commander, _ = links

    with pytest.raises(UnknownFollowerError):
        commander.send_command("nobody", "HOLD")


def test_a_follower_can_only_be_registered_once(links):
    commander, _ = links
    left, _ = InMemoryTransport.pair()

    with pytest.raises(CommanderError):
        commander.add_follower("drone-a", left)


def test_removed_follower_gets_no_more_commands(links):
    commander, _ = links

    commander.remove_follower("drone-b")

    assert commander.followers == ["drone-a", "drone-c"]
    assert set(commander.broadcast("HOLD").sent) == {"drone-a", "drone-c"}


def test_a_closed_link_raises_a_transport_error(links):
    commander, followers = links
    followers["drone-a"].link.close()

    with pytest.raises(TransportClosedError):
        commander.send_command("drone-a", "HOLD")


def test_without_a_validator_the_project_validator_is_required():
    try:
        validator = default_validator()
    except CommanderError as error:
        assert "validator" in str(error)
    else:
        assert callable(validator)


def test_broadcast_continues_past_a_dead_link(links):
    commander, followers = links
    followers["drone-b"].link.close()

    result = commander.broadcast("HOLD")

    assert set(result.sent) == {"drone-a", "drone-c"}
    assert isinstance(result.failed["drone-b"], TransportClosedError)
    for follower_id in ("drone-a", "drone-c"):
        assert followers[follower_id].receive() == result.sent[follower_id]


def test_an_invalid_broadcast_sends_nothing(links):
    commander, followers = links

    with pytest.raises(InvalidCommandError):
        commander.broadcast("PROCEED_TO_WAYPOINT")

    for follower in followers.values():
        with pytest.raises(TransportTimeoutError):
            follower.link.receive(timeout=0)


def test_zero_timeout_still_reads_a_response_that_already_arrived(links):
    commander, followers = links
    sent = commander.send_command("drone-a", "HOLD")
    followers["drone-a"].reply(followers["drone-a"].receive(), "COMPLY")

    assert commander.await_response(sent, timeout=0).outcome is Outcome.COMPLY


def test_a_late_response_is_not_mistaken_for_a_new_one(links):
    commander, followers = links
    follower = followers["drone-a"]
    sent = commander.send_command("drone-a", "HOLD")
    command = follower.receive()
    assert commander.await_response(sent, timeout=0.01).outcome is Outcome.TIMEOUT

    late = follower.reply(command, "COMPLY")
    newer = commander.send_command("drone-a", "RETURN")
    follower.reply(follower.receive(), "COMPLY")

    assert commander.await_response(newer, timeout=1.0).outcome is Outcome.COMPLY
    assert commander.other_messages("drone-a") == [late]


def test_unclaimed_messages_are_bounded(links):
    commander, followers = links
    follower = followers["drone-a"]
    sent = commander.send_command("drone-a", "HOLD")
    command = follower.receive()
    heartbeat = {
        **follower.response(command, "COMPLY"),
        "type": "heartbeat",
        "payload": {"status": {"battery_percent": 80}},
    }
    for _ in range(KEEP_PER_FOLLOWER + 50):
        follower.link.send(heartbeat)
    follower.reply(command, "COMPLY")

    assert commander.await_response(sent, timeout=1.0).outcome is Outcome.COMPLY
    assert len(commander.other_messages("drone-a")) == KEEP_PER_FOLLOWER


@pytest.fixture
def single_link():
    """One connected link: the commander end and a scripted follower."""
    commander_end, follower_end = InMemoryTransport.pair()
    commander_end.connect()
    follower_end.connect()
    yield commander_end, Follower("drone-a", follower_end)
    commander_end.close()
    follower_end.close()


def test_a_lax_validator_cannot_make_a_bad_response_crash_the_commander(single_link):
    commander_end, follower = single_link
    commander = Commander("ground", validator=lambda message: (True, []))
    commander.add_follower("drone-a", commander_end)

    for bad_payload in (
        "not an object",
        {"response": "MAYBE"},
        {"response": "COMPLY", "in_reply_to": ["a", "list"]},
    ):
        sent = commander.send_command("drone-a", "HOLD")
        command = follower.receive()
        follower.link.send(
            {**follower.response(command, "COMPLY"), "payload": bad_payload}
        )
        follower.reply(command, "COMPLY")

        result = commander.await_response(sent, timeout=1.0)

        assert result.outcome is Outcome.COMPLY
    assert len(commander.rejected_responses("drone-a")) == 3


def test_a_validator_that_throws_on_a_response_does_not_crash_the_commander(
    single_link,
):
    commander_end, follower = single_link

    def validate(message):
        if message["type"] == "response":
            raise RuntimeError("boom")
        return VALIDATE(message)

    commander = Commander("ground", validator=validate)
    commander.add_follower("drone-a", commander_end)
    sent = commander.send_command("drone-a", "HOLD")
    follower.reply(follower.receive(), "COMPLY")

    result = commander.await_response(sent, timeout=1.0)

    assert result.outcome is Outcome.INVALID_RESPONSE
    assert any("boom" in problem for problem in result.problems)
