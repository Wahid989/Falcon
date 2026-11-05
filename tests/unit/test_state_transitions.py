"""Tests for deterministic follower command state transitions."""

from dataclasses import fields

import pytest

from src.state import (
    CommandLifecycle,
    FollowerState,
    apply_command_transition,
)


@pytest.mark.parametrize(
    ("command", "payload", "initial", "expected"),
    [
        (
            "HOLD",
            None,
            {"movement_flag": True, "current_command": "RETURN"},
            {"movement_flag": False, "current_command": None},
        ),
        (
            "PROCEED_TO_WAYPOINT",
            {"waypoint": "north-fence"},
            {},
            {
                "position": "north-fence",
                "movement_flag": True,
                "current_command": "PROCEED_TO_WAYPOINT",
            },
        ),
        (
            "RETURN",
            None,
            {"position": "north-fence"},
            {"position": "base", "movement_flag": True, "current_command": "RETURN"},
        ),
        (
            "REPORT_STATUS",
            None,
            {"battery_percent": 42, "position": "north-fence"},
            {"battery_percent": 42, "position": "north-fence"},
        ),
    ],
)
def test_complied_commands_apply_documented_effects(
    command, payload, initial, expected
):
    state = FollowerState(**initial)
    expected_state = FollowerState(**initial)
    expected_state.force_state(expected)

    result = apply_command_transition(state, command, payload, complied=True)

    assert result.state == expected_state
    assert result.lifecycle == (
        CommandLifecycle.READY,
        CommandLifecycle.COMMAND_RECEIVED,
        CommandLifecycle.EVALUATING,
        CommandLifecycle.COMPLIED,
    )
    assert state == FollowerState(**initial)


@pytest.mark.parametrize(
    ("command", "payload"),
    [
        ("HOLD", None),
        ("PROCEED_TO_WAYPOINT", {"waypoint": "north-fence"}),
        ("RETURN", None),
        ("REPORT_STATUS", None),
    ],
)
def test_declined_commands_preserve_all_domain_fields(command, payload):
    state = FollowerState(
        battery_percent=12,
        obstacle_detected=True,
        mechanical_fault=True,
        terrain="rocky",
        capabilities=["camera"],
        position="north-fence",
        movement_flag=True,
        current_command="existing-command",
        current_role="commander",
        role_epoch=3,
        hierarchy_level=2,
        parent_id="parent-1",
        follower_ids=["child-1"],
    )
    original = FollowerState(
        **{field.name: getattr(state, field.name) for field in fields(state)}
    )

    result = apply_command_transition(state, command, payload, complied=False)

    assert result.state == original
    assert result.state is not state
    assert result.lifecycle == (
        CommandLifecycle.READY,
        CommandLifecycle.COMMAND_RECEIVED,
        CommandLifecycle.EVALUATING,
        CommandLifecycle.DECLINED,
    )


def test_transitions_are_deterministic_and_do_not_mutate_input():
    state = FollowerState(capabilities=["camera"], position="base")

    first = apply_command_transition(
        state, "PROCEED_TO_WAYPOINT", {"waypoint": "north-fence"}, complied=True
    )
    second = apply_command_transition(
        state, "PROCEED_TO_WAYPOINT", {"waypoint": "north-fence"}, complied=True
    )

    assert first == second
    assert first.state == FollowerState(
        capabilities=["camera"],
        position="north-fence",
        movement_flag=True,
        current_command="PROCEED_TO_WAYPOINT",
    )
    assert state.position == "base"
    assert state.movement_flag is False
    assert state.current_command is None


@pytest.mark.parametrize(
    ("command", "payload", "error"),
    [
        ("UNKNOWN", None, ValueError),
        ("PROCEED_TO_WAYPOINT", None, ValueError),
        ("PROCEED_TO_WAYPOINT", {"waypoint": ""}, ValueError),
        ("PROCEED_TO_WAYPOINT", {"waypoint": 5}, ValueError),
        ("HOLD", {"waypoint": "north-fence"}, ValueError),
        ("RETURN", {"unexpected": True}, ValueError),
    ],
)
def test_invalid_commands_and_payloads_are_rejected(command, payload, error):
    with pytest.raises(error):
        apply_command_transition(FollowerState(), command, payload, complied=True)
