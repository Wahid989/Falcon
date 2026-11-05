"""In-memory Commander-to-Follower round trips.

The test validator checks the real protocol schema directly because the
FALCON-019 production validator is not available on this branch. Decision
callbacks below are deterministic test substitutes, not Phase-4 policy.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from src.agents import Commander, Follower, Outcome
from src.decision import DecisionResult, DeclineReason
from src.state import FollowerState
from src.transport import InMemoryTransport

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "src" / "schema" / "falcon-v1.json"
with SCHEMA_PATH.open(encoding="utf-8") as schema_file:
    SCHEMA_VALIDATOR = Draft202012Validator(
        json.load(schema_file), format_checker=FormatChecker()
    )


def validate_schema(message):
    errors = sorted(SCHEMA_VALIDATOR.iter_errors(message), key=str)
    return not errors, [error.message for error in errors]


@pytest.fixture
def agent_pair():
    commander_link, follower_link = InMemoryTransport.pair()
    commander_link.connect()
    follower_link.connect()

    commander = Commander("commander-1", validator=validate_schema)
    commander.add_follower("follower-1", commander_link)
    state = FollowerState(parent_id="commander-1")

    yield commander, follower_link, state

    commander_link.close()
    follower_link.close()


def round_trip(agent_pair, command, decision, waypoint=None, state=None):
    commander, follower_link, default_state = agent_pair
    state = state if state is not None else default_state
    follower = Follower(
        agent_id="follower-1",
        transport=follower_link,
        state=state,
        decision_engine=lambda *_: decision,
        validator=validate_schema,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        follower_future = executor.submit(follower.handle_once, timeout=1.0)
        commander_result = commander.issue(
            "follower-1",
            command,
            waypoint=waypoint,
            timeout=1.0,
        )
        follower_result = follower_future.result(timeout=1.0)

    assert follower_result is not None
    assert commander_result.response is not None
    assert (
        commander_result.response["payload"]["in_reply_to"]
        == commander_result.command["message_id"]
    )
    assert validate_schema(commander_result.response)[0]
    return commander_result, follower_result, follower


@pytest.mark.parametrize(
    ("command", "waypoint", "initial", "expected"),
    [
        (
            "HOLD",
            None,
            {"movement_flag": True, "current_command": "RETURN"},
            {"movement_flag": False, "current_command": None},
        ),
        (
            "PROCEED_TO_WAYPOINT",
            "north-fence",
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
            {"battery_percent": 37, "position": "north-fence"},
            {"battery_percent": 37, "position": "north-fence"},
        ),
    ],
)
def test_complied_commands_round_trip_and_apply_expected_state(
    agent_pair, command, waypoint, initial, expected
):
    _, _, default_state = agent_pair
    state = FollowerState(parent_id=default_state.parent_id, **initial)

    commander_result, follower_result, follower = round_trip(
        agent_pair, command, DecisionResult(True), waypoint=waypoint, state=state
    )

    assert commander_result.outcome is (
        Outcome.STATUS_RESPONSE if command == "REPORT_STATUS" else Outcome.COMPLY
    )
    assert follower_result.decision == DecisionResult(True)
    for field, value in expected.items():
        assert getattr(follower.state, field) == value

    if command == "REPORT_STATUS":
        assert commander_result.state == follower.status_snapshot()
        assert "hierarchy_level" not in commander_result.state
        assert follower.state.position == "north-fence"


def test_declined_command_round_trip_preserves_domain_state(agent_pair):
    _, _, default_state = agent_pair
    state = FollowerState(
        parent_id=default_state.parent_id,
        battery_percent=12,
        obstacle_detected=True,
        mechanical_fault=True,
        terrain="rocky",
        capabilities=["camera"],
        position="north-fence",
        movement_flag=True,
        current_command="existing-command",
    )
    before = FollowerState(
        **{field: getattr(state, field) for field in FollowerState.__dataclass_fields__}
    )
    declined = DecisionResult(False, DeclineReason.BATTERY_CRITICAL)

    commander_result, follower_result, follower = round_trip(
        agent_pair,
        "RETURN",
        declined,
        state=state,
    )

    assert commander_result.outcome is Outcome.DECLINE
    assert commander_result.reason == DeclineReason.BATTERY_CRITICAL.value
    assert follower_result.decision == declined
    assert follower.state == before
