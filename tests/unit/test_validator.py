"""Unit tests for the FALCON message validator (FALCON-019).

Covers valid messages of every type and invalid messages for every envelope
field, asserting that the validator names the offending field. Exercises the
same ``validate`` used on both the send and receive sides.
"""

import pytest

from src.schema.validator import is_valid, validate


def envelope(message_type, payload, **overrides):
    base = {
        "message_id": "11111111-1111-1111-1111-111111111111",
        "sender_id": "agent-01",
        "recipient_id": "agent-02",
        "timestamp": "2026-01-01T00:00:00Z",
        "schema_version": 1,
        "sender_role": "commander",
        "role_epoch": 0,
        "type": message_type,
        "payload": payload,
    }
    base.update(overrides)
    return base


STATUS = {
    "agent_id": "agent-02",
    "battery_percent": 80,
    "obstacle_detected": False,
    "mechanical_fault": False,
    "terrain": "flat",
    "capabilities": ["camera"],
    "position": "base",
    "movement_flag": False,
    "current_command": None,
    "current_role": "follower",
    "role_epoch": 0,
}

# --- Valid messages: validate -> (True, []) -------------------------------- #

VALID = {
    "command_hold": envelope("command", {"command": "HOLD"}),
    "command_proceed": envelope(
        "command", {"command": "PROCEED_TO_WAYPOINT", "waypoint": "north-fence"}
    ),
    "command_return": envelope("command", {"command": "RETURN"}),
    "command_report": envelope("command", {"command": "REPORT_STATUS"}),
    "response_comply": envelope(
        "response", {"response": "COMPLY", "in_reply_to": "m-1"}
    ),
    "response_decline": envelope(
        "response",
        {"response": "DECLINE", "in_reply_to": "m-1", "reason": "BATTERY_CRITICAL"},
    ),
    "response_status": envelope(
        "response",
        {"response": "STATUS_RESPONSE", "in_reply_to": "m-1", "state": STATUS},
        sender_role="follower",
    ),
    "response_error": envelope(
        "response",
        {
            "response": "ERROR",
            "in_reply_to": "m-1",
            "error": {"code": "MISSING_FIELD", "field": "timestamp"},
        },
    ),
    "role_request": envelope("role_request", {"target_id": "agent-00"}),
    "role_grant": envelope(
        "role_grant", {"in_reply_to": "r-1", "granted": True, "new_epoch": 1}
    ),
    "role_deny": envelope(
        "role_deny", {"in_reply_to": "r-1", "reason": "not_authorized"}
    ),
    "heartbeat_down": envelope("heartbeat", {"status_summary": [STATUS]}),
    "heartbeat_up": envelope("heartbeat", {"status": STATUS}, sender_role="follower"),
}


@pytest.mark.parametrize("name", sorted(VALID))
def test_valid_messages_pass(name):
    ok, errors = validate(VALID[name])
    assert ok, f"{name} should pass: {errors}"
    assert errors == []
    assert is_valid(VALID[name]) is True


# --- Invalid messages: validate -> (False, errors naming the field) -------- #
# Each entry: (broken message, substring that must appear in the error output).

INVALID = {
    "missing_message_id": (
        {
            k: v
            for k, v in envelope("command", {"command": "HOLD"}).items()
            if k != "message_id"
        },
        "message_id",
    ),
    "wrong_schema_version": (
        envelope("command", {"command": "HOLD"}, schema_version=2),
        "schema_version",
    ),
    "unknown_type": (envelope("teleport", {"command": "HOLD"}), "type"),
    "bad_command_name": (
        envelope("command", {"command": "FLY"}),
        "command",
    ),
    "proceed_without_waypoint": (
        envelope("command", {"command": "PROCEED_TO_WAYPOINT"}),
        "waypoint",
    ),
    "hold_with_waypoint": (
        envelope("command", {"command": "HOLD", "waypoint": "north-fence"}),
        "waypoint",
    ),
    "decline_without_reason": (
        envelope("response", {"response": "DECLINE", "in_reply_to": "m-1"}),
        "reason",
    ),
    "bad_decline_reason": (
        envelope(
            "response",
            {"response": "DECLINE", "in_reply_to": "m-1", "reason": "OUT_OF_SNACKS"},
        ),
        "reason",
    ),
    "response_missing_in_reply_to": (
        envelope("response", {"response": "COMPLY"}),
        "in_reply_to",
    ),
    "bad_sender_role": (
        envelope("command", {"command": "HOLD"}, sender_role="overlord"),
        "sender_role",
    ),
    "negative_epoch": (
        envelope("command", {"command": "HOLD"}, role_epoch=-1),
        "role_epoch",
    ),
    "bad_sender_id": (
        envelope("command", {"command": "HOLD"}, sender_id="agent 01"),
        "sender_id",
    ),
    "hierarchy_level_on_wire": (
        envelope("command", {"command": "HOLD"}, hierarchy_level=3),
        "hierarchy_level",
    ),
    "status_with_hierarchy_level": (
        envelope(
            "response",
            {
                "response": "STATUS_RESPONSE",
                "in_reply_to": "m-1",
                "state": {**STATUS, "hierarchy_level": 2},
            },
            sender_role="follower",
        ),
        "hierarchy_level",
    ),
    "heartbeat_both_shapes": (
        envelope("heartbeat", {"status": STATUS, "status_summary": [STATUS]}),
        "payload",
    ),
    "missing_payload": (
        {
            k: v
            for k, v in envelope("command", {"command": "HOLD"}).items()
            if k != "payload"
        },
        "payload",
    ),
    "bad_error_code": (
        envelope(
            "response",
            {"response": "ERROR", "in_reply_to": "m-1", "error": {"code": "OOPS"}},
        ),
        "code",
    ),
}


@pytest.mark.parametrize("name", sorted(INVALID))
def test_invalid_messages_fail_with_field_named(name):
    message, expected_field = INVALID[name]
    ok, errors = validate(message)
    assert not ok, f"{name} should fail"
    assert errors, f"{name} should report at least one error"
    joined = " | ".join(errors)
    assert expected_field in joined, f"{name}: expected '{expected_field}' in {joined}"
    assert is_valid(message) is False


def test_non_object_message_is_rejected():
    ok, errors = validate(["not", "an", "object"])
    assert not ok
    assert errors


def test_empty_message_names_missing_fields():
    ok, errors = validate({})
    assert not ok
    joined = " | ".join(errors)
    # Every required envelope field should be reported as missing.
    for required in [
        "message_id",
        "sender_id",
        "recipient_id",
        "timestamp",
        "schema_version",
        "sender_role",
        "role_epoch",
        "type",
        "payload",
    ]:
        assert required in joined


def test_no_response_error_names_the_silent_follower():
    # A commander reports a follower that never answered, naming it via error.agent.
    message = envelope(
        "response",
        {
            "response": "ERROR",
            "in_reply_to": "cmd-1",
            "error": {"code": "NO_RESPONSE", "agent": "follower-3"},
        },
        sender_id="drone-commander",
        recipient_id="top-commander",
    )
    ok, errors = validate(message)
    assert ok, errors
    assert is_valid(message)
