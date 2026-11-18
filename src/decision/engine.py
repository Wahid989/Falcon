"""Deterministic command policy for the follower."""

from typing import Mapping

from src.decision.base import DecisionResult, DeclineReason
from src.state import FollowerState

MOVEMENT_COMMANDS = frozenset({"PROCEED_TO_WAYPOINT", "RETURN"})
BATTERY_MOVEMENT_THRESHOLD = 20


def decide(
    state: FollowerState,
    command: str,
    payload: Mapping[str, object],
    message_role_epoch: int,
) -> DecisionResult:
    """Evaluate the command without changing the state."""
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
    if state.current_command is not None and (
        command == "HOLD" or command in MOVEMENT_COMMANDS
    ):
        return DecisionResult(False, DeclineReason.BUSY)
    return DecisionResult(True)
