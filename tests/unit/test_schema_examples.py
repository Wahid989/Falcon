"""Validate example messages against src/schema/falcon-v1.json.

Proves the FALCON v1 schema accepts every well-formed message type and rejects
known-invalid messages (FALCON-015 / FALCON-016). The Python validator module
with a friendly API lands in FALCON-019; this test exercises the schema
artifact directly with jsonschema.
"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "src" / "schema" / "falcon-v1.json"


def load_validator() -> Draft202012Validator:
    with SCHEMA_PATH.open(encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


VALIDATOR = load_validator()


def envelope(message_type, payload, **overrides):
    """Build a well-formed envelope, overridable per test."""
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

INVALID = {
    "missing_message_id": {
        k: v
        for k, v in envelope("command", {"command": "HOLD"}).items()
        if k != "message_id"
    },
    "wrong_schema_version": envelope("command", {"command": "HOLD"}, schema_version=2),
    "unknown_type": envelope("teleport", {"command": "HOLD"}),
    "bad_command_name": envelope("command", {"command": "FLY"}),
    "proceed_without_waypoint": envelope("command", {"command": "PROCEED_TO_WAYPOINT"}),
    "hold_with_waypoint": envelope(
        "command", {"command": "HOLD", "waypoint": "north-fence"}
    ),
    "decline_without_reason": envelope(
        "response", {"response": "DECLINE", "in_reply_to": "m-1"}
    ),
    "bad_decline_reason": envelope(
        "response",
        {"response": "DECLINE", "in_reply_to": "m-1", "reason": "OUT_OF_SNACKS"},
    ),
    "response_missing_in_reply_to": envelope("response", {"response": "COMPLY"}),
    "bad_sender_role": envelope("command", {"command": "HOLD"}, sender_role="overlord"),
    "negative_epoch": envelope("command", {"command": "HOLD"}, role_epoch=-1),
    "extra_envelope_field": envelope("command", {"command": "HOLD"}, hierarchy_level=3),
    "heartbeat_both_shapes": envelope(
        "heartbeat", {"status": STATUS, "status_summary": [STATUS]}
    ),
    "status_with_hierarchy_level": envelope(
        "response",
        {
            "response": "STATUS_RESPONSE",
            "in_reply_to": "m-1",
            "state": {**STATUS, "hierarchy_level": 2},
        },
        sender_role="follower",
    ),
}


@pytest.mark.parametrize("name", sorted(VALID))
def test_valid_messages_pass(name):
    errors = sorted(VALIDATOR.iter_errors(VALID[name]), key=str)
    assert errors == [], f"{name} should be valid: {[e.message for e in errors]}"


@pytest.mark.parametrize("name", sorted(INVALID))
def test_invalid_messages_fail(name):
    assert not VALIDATOR.is_valid(INVALID[name]), f"{name} should be rejected"
