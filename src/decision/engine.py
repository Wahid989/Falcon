"""Deterministic command policy for the follower."""

from functools import partial
from typing import Iterable, Mapping

from src.decision.base import DecisionEngine, DecisionResult, DeclineReason
from src.state import FollowerState
from src.state.suitability import SuitabilityFailure, check_suitability

MOVEMENT_COMMANDS = frozenset({"PROCEED_TO_WAYPOINT", "RETURN"})
BATTERY_MOVEMENT_THRESHOLD = 20
EMPTY_RESTRICTIONS: frozenset[str] = frozenset()


def configured_decision_engine(
    *,
    unsuitable_terrain: Iterable[str] = (),
    check_return_terrain: bool = False,
    required_capabilities: Mapping[str, Iterable[str]] | None = None,
) -> DecisionEngine:
    """Bind local restrictions while preserving the decision callable contract."""
    requirements = {
        command: frozenset(capabilities)
        for command, capabilities in (required_capabilities or {}).items()
    }
    if set(requirements) - (MOVEMENT_COMMANDS | {"HOLD"}):
        raise ValueError(
            "Equipment requirements must target HOLD, PROCEED_TO_WAYPOINT or RETURN"
        )
    return partial(
        _decide,
        unsuitable_terrain=frozenset(unsuitable_terrain),
        check_return_terrain=check_return_terrain,
        required_capabilities=requirements,
    )


def decide(
    state: FollowerState,
    command: str,
    payload: Mapping[str, object],
    message_role_epoch: int,
) -> DecisionResult:
    """Evaluate the command using the default unrestricted configuration."""
    return _decide(
        state,
        command,
        payload,
        message_role_epoch,
        unsuitable_terrain=EMPTY_RESTRICTIONS,
        check_return_terrain=False,
        required_capabilities={},
    )


def _decide(
    state: FollowerState,
    command: str,
    payload: Mapping[str, object],
    message_role_epoch: int,
    *,
    unsuitable_terrain: frozenset[str],
    check_return_terrain: bool,
    required_capabilities: Mapping[str, frozenset[str]],
) -> DecisionResult:
    if not isinstance(state, FollowerState):
        raise TypeError("state must be a FollowerState")
    if not isinstance(command, str):
        raise TypeError("command must be a string")
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    if type(message_role_epoch) is not int:
        raise TypeError("message_role_epoch must be an integer")

    if (
        command in MOVEMENT_COMMANDS
        and state.battery_percent < BATTERY_MOVEMENT_THRESHOLD
    ):
        return DecisionResult(False, DeclineReason.BATTERY_CRITICAL)

    if command in MOVEMENT_COMMANDS and state.mechanical_fault:
        return DecisionResult(False, DeclineReason.MECHANICAL_FAULT)
    if command == "PROCEED_TO_WAYPOINT" and state.obstacle_detected:
        return DecisionResult(False, DeclineReason.OBSTACLE_DETECTED)
    check_terrain = command == "PROCEED_TO_WAYPOINT" or (
        command == "RETURN" and check_return_terrain
    )
    failure = check_suitability(
        state.terrain,
        state.capabilities,
        unsuitable_terrain=unsuitable_terrain if check_terrain else EMPTY_RESTRICTIONS,
        required_capabilities=required_capabilities.get(command, EMPTY_RESTRICTIONS),
    )
    if failure is SuitabilityFailure.TERRAIN:
        return DecisionResult(False, DeclineReason.TERRAIN_UNSUITABLE)
    if failure is SuitabilityFailure.EQUIPMENT:
        return DecisionResult(False, DeclineReason.CAPABILITY_UNAVAILABLE)
    if state.current_command is not None and (
        command == "HOLD" or command in MOVEMENT_COMMANDS
    ):
        return DecisionResult(False, DeclineReason.BUSY)
    return DecisionResult(True)
