"""Deterministic state transitions for accepted follower commands."""

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from src.state.follower_state import FollowerState


class CommandLifecycle(str, Enum):
    """Lifecycle stages for evaluating one command."""

    READY = "READY"
    COMMAND_RECEIVED = "COMMAND_RECEIVED"
    EVALUATING = "EVALUATING"
    COMPLIED = "COMPLIED"
    DECLINED = "DECLINED"


@dataclass(frozen=True, slots=True)
class CommandTransition:
    """Resulting domain state and separate command lifecycle path."""

    state: FollowerState
    lifecycle: tuple[CommandLifecycle, ...]


def apply_command_transition(
    state: FollowerState,
    command: str,
    payload: Mapping[str, object] | None = None,
    *,
    complied: bool,
) -> CommandTransition:
    """Return a copied state after applying a compliant command's effects.

    Decision guards are intentionally outside this function. ``complied``
    represents the decision engine's outcome; declined commands preserve the
    domain state.
    """
    if not isinstance(state, FollowerState):
        raise TypeError("state must be a FollowerState")
    if not isinstance(command, str):
        raise TypeError("command must be a string")
    if type(complied) is not bool:
        raise TypeError("complied must be a boolean")
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")

    allowed_payloads = {
        "HOLD": set(),
        "PROCEED_TO_WAYPOINT": {"waypoint"},
        "RETURN": set(),
        "REPORT_STATUS": set(),
    }
    if command not in allowed_payloads:
        raise ValueError(f"unsupported command: {command}")
    if set(payload) - allowed_payloads[command]:
        raise ValueError(f"unexpected payload field for {command}")
    if command == "PROCEED_TO_WAYPOINT":
        waypoint = payload.get("waypoint")
        if not isinstance(waypoint, str) or not waypoint:
            raise ValueError("PROCEED_TO_WAYPOINT requires a non-empty waypoint")

    next_state = deepcopy(state)
    if complied:
        if command == "HOLD":
            next_state.movement_flag = False
            next_state.current_command = None
        elif command == "PROCEED_TO_WAYPOINT":
            next_state.position = payload["waypoint"]
            next_state.movement_flag = True
            next_state.current_command = command
        elif command == "RETURN":
            next_state.position = "base"
            next_state.movement_flag = True
            next_state.current_command = command

    outcome = CommandLifecycle.COMPLIED if complied else CommandLifecycle.DECLINED
    return CommandTransition(
        state=next_state,
        lifecycle=(
            CommandLifecycle.READY,
            CommandLifecycle.COMMAND_RECEIVED,
            CommandLifecycle.EVALUATING,
            outcome,
        ),
    )
