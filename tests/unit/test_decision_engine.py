"""Tests for deterministic follower command decisions."""

import copy

import pytest

from src.decision import DecisionResult, DeclineReason, decide
from src.state import FollowerState


@pytest.mark.parametrize("command", ["PROCEED_TO_WAYPOINT", "RETURN"])
@pytest.mark.parametrize(
    ("battery_percent", "expected"),
    [
        (0, DecisionResult(False, DeclineReason.BATTERY_CRITICAL)),
        (5, DecisionResult(False, DeclineReason.BATTERY_CRITICAL)),
        (19, DecisionResult(False, DeclineReason.BATTERY_CRITICAL)),
        (20, DecisionResult(True)),
        (21, DecisionResult(True)),
        (100, DecisionResult(True)),
    ],
)
def test_movement_commands_use_documented_battery_threshold(
    command, battery_percent, expected
):
    state = FollowerState(battery_percent=battery_percent)
    before = copy.deepcopy(state)

    result = decide(
        state,
        command,
        {"waypoint": "north-fence"} if command == "PROCEED_TO_WAYPOINT" else {},
        0,
    )

    assert result == expected
    assert state == before


@pytest.mark.parametrize("command", ["HOLD", "REPORT_STATUS"])
@pytest.mark.parametrize("battery_percent", [0, 5, 19])
def test_non_movement_commands_are_not_blocked_by_low_battery(command, battery_percent):
    state = FollowerState(battery_percent=battery_percent)
    before = copy.deepcopy(state)

    result = decide(state, command, {}, 0)

    assert result == DecisionResult(True)
    assert state == before


def test_healthy_hold_complies():
    assert decide(FollowerState(), "HOLD", {}, 0) == DecisionResult(True)


@pytest.mark.parametrize(
    ("command", "overrides", "reason"),
    [
        ("RETURN", {"mechanical_fault": True}, DeclineReason.MECHANICAL_FAULT),
        (
            "PROCEED_TO_WAYPOINT",
            {"mechanical_fault": True},
            DeclineReason.MECHANICAL_FAULT,
        ),
        (
            "PROCEED_TO_WAYPOINT",
            {"obstacle_detected": True},
            DeclineReason.OBSTACLE_DETECTED,
        ),
        ("RETURN", {"obstacle_detected": True}, None),
        ("RETURN", {"current_command": "RETURN"}, DeclineReason.BUSY),
        (
            "PROCEED_TO_WAYPOINT",
            {"current_command": "RETURN"},
            DeclineReason.BUSY,
        ),
        ("HOLD", {"current_command": "RETURN"}, DeclineReason.BUSY),
        ("HOLD", {"battery_percent": 5, "terrain": "rocky"}, None),
        (
            "REPORT_STATUS",
            {
                "battery_percent": 5,
                "mechanical_fault": True,
                "obstacle_detected": True,
                "current_command": "RETURN",
            },
            None,
        ),
    ],
)
def test_state_guards_and_command_exceptions(command, overrides, reason):
    state = FollowerState(**overrides)
    before = copy.deepcopy(state)
    payload = {"waypoint": "north-fence"} if command == "PROCEED_TO_WAYPOINT" else {}

    assert decide(state, command, payload, 0) == DecisionResult(reason is None, reason)
    assert state == before


@pytest.mark.parametrize(
    ("fault", "obstacle", "busy", "return_reason", "proceed_reason"),
    [
        (True, False, False, "MECHANICAL_FAULT", "MECHANICAL_FAULT"),
        (False, True, False, None, "OBSTACLE_DETECTED"),
        (False, False, True, "BUSY", "BUSY"),
        (True, True, False, "MECHANICAL_FAULT", "MECHANICAL_FAULT"),
        (True, False, True, "MECHANICAL_FAULT", "MECHANICAL_FAULT"),
        (False, True, True, "BUSY", "OBSTACLE_DETECTED"),
        (True, True, True, "MECHANICAL_FAULT", "MECHANICAL_FAULT"),
    ],
)
@pytest.mark.parametrize("battery_percent", [19, 20])
@pytest.mark.parametrize("command", ["RETURN", "PROCEED_TO_WAYPOINT"])
def test_competing_guards_at_battery_threshold(
    command, battery_percent, fault, obstacle, busy, return_reason, proceed_reason
):
    state = FollowerState(
        battery_percent=battery_percent,
        mechanical_fault=fault,
        obstacle_detected=obstacle,
        current_command="RETURN" if busy else None,
        position="east-ridge",
        movement_flag=True,
    )
    before = copy.deepcopy(state)
    payload = {"waypoint": "north-fence"} if command == "PROCEED_TO_WAYPOINT" else {}
    expected = return_reason if command == "RETURN" else proceed_reason
    if battery_percent == 19:
        expected = "BATTERY_CRITICAL"
    reason = DeclineReason(expected) if expected else None

    assert decide(state, command, payload, 0) == DecisionResult(reason is None, reason)
    assert state == before


def test_decision_rejects_non_state_and_non_mapping_inputs():
    with pytest.raises(TypeError, match="state must be a FollowerState"):
        decide(object(), "HOLD", {}, 0)
    with pytest.raises(TypeError, match="payload must be a mapping"):
        decide(FollowerState(), "HOLD", [], 0)
