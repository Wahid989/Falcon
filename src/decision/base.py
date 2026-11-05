"""Minimal decision contract shared with the agent runtime."""

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol

from src.state import FollowerState


class DeclineReason(str, Enum):
    """Fixed reasons a valid command may be declined."""

    BATTERY_CRITICAL = "BATTERY_CRITICAL"
    OBSTACLE_DETECTED = "OBSTACLE_DETECTED"
    MECHANICAL_FAULT = "MECHANICAL_FAULT"
    BUSY = "BUSY"
    INVALID_REQUEST = "INVALID_REQUEST"
    STALE_EPOCH = "STALE_EPOCH"
    TERRAIN_UNSUITABLE = "TERRAIN_UNSUITABLE"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """Decision outcome without implementing decision policy."""

    complied: bool
    reason: DeclineReason | None = None

    def __post_init__(self) -> None:
        if type(self.complied) is not bool:
            raise TypeError("complied must be a boolean")
        if self.complied and self.reason is not None:
            raise ValueError("a compliant decision cannot include a decline reason")
        if not self.complied and not isinstance(self.reason, DeclineReason):
            raise ValueError("a declined decision requires a DeclineReason")


class DecisionEngine(Protocol):
    """Callable interface for a future decision-engine implementation."""

    def __call__(
        self,
        state: FollowerState,
        command: str,
        payload: Mapping[str, object],
        message_role_epoch: int,
    ) -> DecisionResult:
        """Evaluate a command without applying state transitions."""
