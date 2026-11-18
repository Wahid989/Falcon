"""Tests for receiving and responding to FALCON command messages."""

import json
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

import pytest

from src.agents import Follower, InvalidFollowerMessageError
from src.decision import DecisionResult, DeclineReason, configured_decision_engine
from src.state import CommandLifecycle, FollowerState
from src.transport import InMemoryTransport


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "src" / "schema" / "falcon-v1.json"
with SCHEMA_PATH.open(encoding="utf-8") as schema_file:
    VALIDATOR = Draft202012Validator(json.load(schema_file))


def validate(message):
    errors = sorted(VALIDATOR.iter_errors(message), key=str)
    return not errors, [error.message for error in errors]


def command_envelope(command, payload=None, **overrides):
    message = {
        "message_id": "command-1",
        "sender_id": "commander-1",
        "recipient_id": "follower-1",
        "timestamp": "2026-01-01T00:00:00Z",
        "schema_version": 1,
        "sender_role": "commander",
        "role_epoch": 0,
        "type": "command",
        "payload": {"command": command, **(payload or {})},
    }
    message.update(overrides)
    return message


@pytest.fixture
def connected_pair():
    commander, follower = InMemoryTransport.pair()
    commander.connect()
    follower.connect()
    yield commander, follower
    commander.close()
    follower.close()


def make_follower(transport, decision, state=None):
    return Follower(
        agent_id="follower-1",
        transport=transport,
        decision_engine=decision,
        state=state if state is not None else FollowerState(parent_id="commander-1"),
        validator=validate,
    )


@pytest.mark.parametrize(
    ("command", "payload", "state_overrides", "decision", "expected_state"),
    [
        (
            "HOLD",
            {},
            {"movement_flag": True, "current_command": "RETURN"},
            DecisionResult(True),
            {"movement_flag": False, "current_command": None},
        ),
        (
            "PROCEED_TO_WAYPOINT",
            {"waypoint": "north-fence"},
            {},
            DecisionResult(True),
            {
                "position": "north-fence",
                "movement_flag": True,
                "current_command": "PROCEED_TO_WAYPOINT",
            },
        ),
        (
            "RETURN",
            {},
            {"position": "north-fence"},
            DecisionResult(True),
            {"position": "base", "movement_flag": True, "current_command": "RETURN"},
        ),
        (
            "REPORT_STATUS",
            {},
            {"battery_percent": 42, "position": "north-fence"},
            DecisionResult(True),
            {"battery_percent": 42, "position": "north-fence"},
        ),
    ],
)
def test_complied_command_effects_and_correlated_responses(
    connected_pair, command, payload, state_overrides, decision, expected_state
):
    commander, link = connected_pair
    state = FollowerState(parent_id="commander-1", **state_overrides)
    expected = FollowerState(parent_id="commander-1", **state_overrides)
    expected.force_state(expected_state)
    follower = make_follower(link, lambda *_: decision, state)
    commander.send(command_envelope(command, payload))

    result = follower.handle_once(timeout=0)
    response = commander.receive(timeout=0)

    assert result is not None
    assert result.request["message_id"] == "command-1"
    assert result.lifecycle == (
        CommandLifecycle.READY,
        CommandLifecycle.COMMAND_RECEIVED,
        CommandLifecycle.EVALUATING,
        CommandLifecycle.COMPLIED,
    )
    assert follower.state == expected
    for field, value in expected_state.items():
        assert getattr(follower.state, field) == value
    assert response["payload"]["in_reply_to"] == "command-1"
    if command == "REPORT_STATUS":
        assert response["payload"]["response"] == "STATUS_RESPONSE"
        assert "hierarchy_level" not in response["payload"]["state"]
        assert validate(response)[0]
    else:
        assert response["payload"]["response"] == "COMPLY"
        assert validate(response)[0]


def test_decline_preserves_domain_state_and_returns_reason(connected_pair):
    commander, link = connected_pair
    state = FollowerState(
        parent_id="commander-1",
        battery_percent=12,
        position="north-fence",
        movement_flag=True,
        current_command="existing",
        capabilities=["camera"],
    )
    before = FollowerState(
        **{field: getattr(state, field) for field in FollowerState.__dataclass_fields__}
    )
    follower = make_follower(
        link,
        lambda *_: DecisionResult(False, DeclineReason.BATTERY_CRITICAL),
        state,
    )
    commander.send(command_envelope("RETURN"))

    result = follower.handle_once(timeout=0)
    response = commander.receive(timeout=0)

    assert result is not None
    assert result.lifecycle[-1] is CommandLifecycle.DECLINED
    assert follower.state == before
    assert response["payload"]["response"] == "DECLINE"
    assert response["payload"]["reason"] == "BATTERY_CRITICAL"
    assert validate(response)[0]


def test_decision_receives_copied_state_payload_and_role_epoch(connected_pair):
    commander, link = connected_pair
    state = FollowerState(parent_id="commander-1", position="base")
    seen = []

    def decide(decision_state, command, payload, role_epoch):
        seen.append((decision_state, command, payload, role_epoch))
        decision_state.position = "decision-side-effect"
        return DecisionResult(True)

    follower = make_follower(link, decide, state)
    commander.send(command_envelope("HOLD", role_epoch=4))
    follower.handle_once(timeout=0)

    assert seen[0][1:] == ("HOLD", {}, 4)
    assert seen[0][0] is not state
    assert state.position == "base"


@pytest.mark.parametrize(
    ("overrides", "error_code"),
    [
        ({"recipient_id": "another-agent"}, "INVALID_SENDER_OR_RECIPIENT"),
        ({"sender_id": "stranger"}, "UNAUTHORIZED"),
        ({"sender_role": "follower"}, "UNAUTHORIZED"),
    ],
)
def test_authority_failures_send_error_without_deciding(
    connected_pair, overrides, error_code
):
    commander, link = connected_pair
    calls = []
    follower = make_follower(link, lambda *args: calls.append(args))
    commander.send(command_envelope("HOLD", **overrides))

    result = follower.handle_once(timeout=0)
    response = commander.receive(timeout=0)

    assert result is not None
    assert result.decision is None
    assert calls == []
    assert response["payload"]["response"] == "ERROR"
    assert response["payload"]["error"]["code"] == error_code
    assert validate(response)[0]


def test_schema_invalid_command_gets_protocol_error(connected_pair):
    commander, link = connected_pair
    follower = make_follower(link, lambda *_: pytest.fail("decision must not run"))
    commander.send(command_envelope("FLY"))

    result = follower.handle_once(timeout=0)
    response = commander.receive(timeout=0)

    assert result is not None
    assert result.decision is None
    assert response["payload"]["error"]["code"] == "UNSUPPORTED_COMMAND"
    assert response["payload"]["in_reply_to"] == "command-1"
    assert validate(response)[0]


def test_stale_epoch_gets_error_without_calling_decision(connected_pair):
    commander, link = connected_pair
    calls = []
    follower = make_follower(
        link,
        lambda *args: calls.append(args),
        FollowerState(parent_id="commander-1", role_epoch=2),
    )
    commander.send(command_envelope("HOLD", role_epoch=1))

    result = follower.handle_once(timeout=0)
    response = commander.receive(timeout=0)

    assert result is not None
    assert result.decision is None
    assert calls == []
    assert response["payload"]["response"] == "ERROR"
    assert response["payload"]["error"]["code"] == "STALE_EPOCH"
    assert response["payload"]["in_reply_to"] == "command-1"
    assert validate(response)[0]


def test_invalid_sender_id_does_not_get_an_invalid_error_response(connected_pair):
    commander, link = connected_pair
    follower = make_follower(link, lambda *_: pytest.fail("decision must not run"))
    commander.send(command_envelope("HOLD", sender_id="invalid sender"))

    with pytest.raises(InvalidFollowerMessageError, match="invalid sender_id"):
        follower.handle_once(timeout=0)

    assert follower.state == FollowerState(parent_id="commander-1")


def test_non_command_message_is_ignored(connected_pair):
    commander, link = connected_pair
    follower = make_follower(link, lambda *_: pytest.fail("decision must not run"))
    heartbeat = {
        "message_id": "heartbeat-1",
        "sender_id": "commander-1",
        "recipient_id": "follower-1",
        "timestamp": "2026-01-01T00:00:00Z",
        "schema_version": 1,
        "sender_role": "commander",
        "role_epoch": 0,
        "type": "heartbeat",
        "payload": {"status": {"battery_percent": 100}},
    }
    commander.send(heartbeat)

    assert follower.handle_once(timeout=0) is None


def test_busy_hold_declines_with_real_policy_and_preserves_state(connected_pair):
    commander, link = connected_pair
    state = FollowerState(
        parent_id="commander-1", current_command="RETURN", movement_flag=True
    )
    follower = Follower(agent_id="follower-1", transport=link, state=state)
    commander.send(command_envelope("HOLD"))

    follower.handle_once(timeout=0)
    response = commander.receive(timeout=0)

    assert response["payload"] == {
        "response": "DECLINE",
        "reason": "BUSY",
        "in_reply_to": "command-1",
    }
    assert follower.state == FollowerState(
        parent_id="commander-1", current_command="RETURN", movement_flag=True
    )


@pytest.mark.parametrize(
    ("command", "payload", "epoch", "overrides", "outcome", "reason"),
    [
        ("FLY", {}, 1, {}, "ERROR", "UNSUPPORTED_COMMAND"),
        ("PROCEED_TO_WAYPOINT", {}, 2, {}, "ERROR", "MISSING_FIELD"),
        ("PROCEED_TO_WAYPOINT", {"waypoint": "ridge"}, 1, {}, "ERROR", "STALE_EPOCH"),
        (
            "PROCEED_TO_WAYPOINT",
            {"waypoint": "ridge"},
            2,
            {},
            "DECLINE",
            "BATTERY_CRITICAL",
        ),
        (
            "PROCEED_TO_WAYPOINT",
            {"waypoint": "ridge"},
            2,
            {"battery_percent": 20},
            "DECLINE",
            "MECHANICAL_FAULT",
        ),
        (
            "PROCEED_TO_WAYPOINT",
            {"waypoint": "ridge"},
            2,
            {"battery_percent": 20, "mechanical_fault": False},
            "DECLINE",
            "OBSTACLE_DETECTED",
        ),
        (
            "PROCEED_TO_WAYPOINT",
            {"waypoint": "ridge"},
            2,
            {
                "battery_percent": 20,
                "mechanical_fault": False,
                "obstacle_detected": False,
            },
            "DECLINE",
            "TERRAIN_UNSUITABLE",
        ),
        (
            "PROCEED_TO_WAYPOINT",
            {"waypoint": "ridge"},
            2,
            {
                "battery_percent": 20,
                "mechanical_fault": False,
                "obstacle_detected": False,
                "terrain": "available",
            },
            "DECLINE",
            "CAPABILITY_UNAVAILABLE",
        ),
        (
            "PROCEED_TO_WAYPOINT",
            {"waypoint": "ridge"},
            2,
            {
                "battery_percent": 20,
                "mechanical_fault": False,
                "obstacle_detected": False,
                "terrain": "available",
                "capabilities": ["fixture-item"],
            },
            "DECLINE",
            "BUSY",
        ),
        ("REPORT_STATUS", {}, 2, {}, "STATUS_RESPONSE", None),
        (
            "PROCEED_TO_WAYPOINT",
            {"waypoint": "ridge"},
            2,
            {
                "battery_percent": 20,
                "mechanical_fault": False,
                "obstacle_detected": False,
                "terrain": "available",
                "capabilities": ["fixture-item"],
                "current_command": None,
            },
            "COMPLY",
            None,
        ),
    ],
)
def test_real_follower_guard_precedence(
    connected_pair, command, payload, epoch, overrides, outcome, reason
):
    commander, link = connected_pair
    state = FollowerState(
        parent_id="commander-1",
        role_epoch=2,
        **{
            "battery_percent": 19,
            "mechanical_fault": True,
            "obstacle_detected": True,
            "terrain": "blocked",
            "current_command": "RETURN",
            "movement_flag": True,
            "position": "east-ridge",
            **overrides,
        },
    )
    before = deepcopy(state)
    policy = configured_decision_engine(
        unsuitable_terrain={"blocked"},
        required_capabilities={"PROCEED_TO_WAYPOINT": {"fixture-item"}},
    )
    follower = Follower(
        agent_id="follower-1", transport=link, state=state, decision_engine=policy
    )
    commander.send(command_envelope(command, payload, role_epoch=epoch))

    result = follower.handle_once(timeout=0)
    response = commander.receive(timeout=0)
    assert validate(response)[0]
    assert response["payload"]["in_reply_to"] == "command-1"
    assert response["payload"]["response"] == outcome
    if outcome == "ERROR":
        assert response["payload"]["error"]["code"] == reason
        assert result.decision is None
    elif outcome == "DECLINE":
        assert response["payload"]["reason"] == reason
        assert result.decision == DecisionResult(False, DeclineReason(reason))
    elif outcome == "STATUS_RESPONSE":
        assert response["payload"]["state"] == follower.status_snapshot()
    else:
        assert result.decision == DecisionResult(True)
        before.position = "ridge"
        before.current_command = "PROCEED_TO_WAYPOINT"
    assert follower.state == before


def test_default_validator_uses_falcon_019_module(connected_pair):
    _, link = connected_pair

    # FALCON-019 is now merged, so the default validator is the real schema
    # validator and the Follower constructs without an injected validator.
    from src.agents.follower import default_validator
    from src.schema.validator import validate as falcon_validate

    assert default_validator() is falcon_validate
    follower = Follower(
        agent_id="follower-1",
        transport=link,
        decision_engine=lambda *_: DecisionResult(True),
        state=FollowerState(parent_id="commander-1"),
    )
    assert follower is not None
