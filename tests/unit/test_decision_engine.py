"""Tests for the deterministic FALCON-031 battery decision."""

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


def test_decision_rejects_non_state_and_non_mapping_inputs():
    with pytest.raises(TypeError, match="state must be a FollowerState"):
        decide(object(), "HOLD", {}, 0)
    with pytest.raises(TypeError, match="payload must be a mapping"):
        decide(FollowerState(), "HOLD", [], 0)
