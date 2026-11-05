"""Tests for the validated simulated follower state."""

from pathlib import Path

import pytest

from src.state import FollowerState, load_scenario


def test_fresh_state_has_documented_defaults():
    assert FollowerState() == FollowerState(
        battery_percent=100,
        obstacle_detected=False,
        mechanical_fault=False,
        terrain="flat",
        capabilities=[],
        position="base",
        movement_flag=False,
        current_command=None,
        current_role="follower",
        role_epoch=0,
        hierarchy_level=0,
        parent_id=None,
        follower_ids=[],
    )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("battery_percent", -1, ValueError),
        ("battery_percent", 101, ValueError),
        ("battery_percent", True, TypeError),
        ("obstacle_detected", 1, TypeError),
        ("mechanical_fault", "false", TypeError),
        ("terrain", None, TypeError),
        ("capabilities", "camera", TypeError),
        ("capabilities", ["camera", 1], TypeError),
        ("position", 12, TypeError),
        ("movement_flag", 0, TypeError),
        ("current_command", 1, TypeError),
        ("current_role", "Follower", ValueError),
        ("role_epoch", -1, ValueError),
        ("role_epoch", False, TypeError),
        ("hierarchy_level", -1, ValueError),
        ("parent_id", 1, TypeError),
        ("follower_ids", [None], TypeError),
    ],
)
def test_invalid_values_are_rejected_during_initialization(field, value, error):
    with pytest.raises(error):
        FollowerState(**{field: value})


def test_assignment_validates_and_copies_list_fields():
    state = FollowerState()
    capabilities = ["camera"]

    state.capabilities = capabilities
    capabilities.append("radio")

    assert state.capabilities == ["camera"]
    with pytest.raises(ValueError):
        state.battery_percent = 101
    assert state.battery_percent == 100


def test_force_state_updates_fields_and_rejects_invalid_overrides_atomically():
    state = FollowerState()
    state.force_state({"battery_percent": 15, "obstacle_detected": True})

    assert state.battery_percent == 15
    assert state.obstacle_detected is True

    with pytest.raises(ValueError, match="battery_percent"):
        state.force_state({"position": "north-fence", "battery_percent": 101})

    assert state.position == "base"
    assert state.battery_percent == 15


def test_force_state_rejects_unknown_fields():
    with pytest.raises(ValueError, match="unknown follower state field"):
        FollowerState().force_state({"battery": 25})


def test_load_scenario_reads_state_overrides():
    scenario_path = (
        Path(__file__).resolve().parents[1] / "scenarios" / "follower_low_battery.yml"
    )

    assert load_scenario(scenario_path) == FollowerState(battery_percent=15)


@pytest.mark.parametrize("contents", ["", "- not-a-mapping", "battery_percent: 101"])
def test_load_scenario_rejects_non_mappings_and_invalid_state(tmp_path, contents):
    scenario_path = tmp_path / "invalid.yml"
    scenario_path.write_text(contents, encoding="utf-8")

    with pytest.raises(ValueError):
        load_scenario(scenario_path)
