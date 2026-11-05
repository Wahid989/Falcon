"""FALCON-031 MVP round-trip message contract (Wahid's lane).

Asserts that the exact HOLD / COMPLY / DECLINE(BATTERY_CRITICAL) envelopes used
by the go/no-go round trip are valid under the v1 schema, and that the shared
send/receive guard accepts them and rejects malformed ones. See
docs/mvp-roundtrip.md.
"""

import pytest

from src.schema.validator import (
    SchemaValidationError,
    validate,
    validate_or_raise,
)


def _envelope(message_type, payload, **overrides):
    base = {
        "message_id": "11111111-1111-1111-1111-111111111111",
        "sender_id": "commander-1",
        "recipient_id": "follower-1",
        "timestamp": "2026-01-01T00:00:00Z",
        "schema_version": 1,
        "sender_role": "commander",
        "role_epoch": 0,
        "type": message_type,
        "payload": payload,
    }
    base.update(overrides)
    return base


HOLD = _envelope("command", {"command": "HOLD"})

COMPLY = _envelope(
    "response",
    {"response": "COMPLY", "in_reply_to": HOLD["message_id"]},
    sender_id="follower-1",
    recipient_id="commander-1",
    sender_role="follower",
)

DECLINE_BATTERY = _envelope(
    "response",
    {
        "response": "DECLINE",
        "in_reply_to": "22222222-2222-2222-2222-222222222222",
        "reason": "BATTERY_CRITICAL",
    },
    sender_id="follower-1",
    recipient_id="commander-1",
    sender_role="follower",
)

ROUND_TRIP = {"hold": HOLD, "comply": COMPLY, "decline_battery": DECLINE_BATTERY}


@pytest.mark.parametrize("name", sorted(ROUND_TRIP))
def test_round_trip_messages_are_valid(name):
    ok, errors = validate(ROUND_TRIP[name])
    assert ok, f"{name} must be contract-valid: {errors}"
    assert validate_or_raise(ROUND_TRIP[name]) is ROUND_TRIP[name]


def test_decline_uses_the_enumerated_battery_reason():
    assert DECLINE_BATTERY["payload"]["reason"] == "BATTERY_CRITICAL"
    ok, _ = validate(DECLINE_BATTERY)
    assert ok


def test_broadcast_targets_each_follower():
    for follower in ("follower-1", "follower-2", "follower-3"):
        message = _envelope("command", {"command": "HOLD"}, recipient_id=follower)
        assert validate_or_raise(message) is message


def test_guard_raises_and_names_field_on_invalid_send():
    # A DECLINE missing its required reason must be rejected before it is sent.
    bad = _envelope(
        "response",
        {"response": "DECLINE", "in_reply_to": HOLD["message_id"]},
        sender_id="follower-1",
        sender_role="follower",
    )
    with pytest.raises(SchemaValidationError) as excinfo:
        validate_or_raise(bad)
    assert any("reason" in error for error in excinfo.value.errors)


def test_guard_rejects_comply_without_in_reply_to():
    bad = _envelope(
        "response",
        {"response": "COMPLY"},
        sender_id="follower-1",
        sender_role="follower",
    )
    with pytest.raises(SchemaValidationError) as excinfo:
        validate_or_raise(bad)
    assert any("in_reply_to" in error for error in excinfo.value.errors)
