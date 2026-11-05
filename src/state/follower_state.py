"""Validated model of an agent's simulated follower state."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping

import yaml


@dataclass(slots=True)
class FollowerState:
    """Mutable follower state with validation on initialization and assignment."""

    battery_percent: int = 100
    obstacle_detected: bool = False
    mechanical_fault: bool = False
    terrain: str = "flat"
    capabilities: list[str] = field(default_factory=list)
    position: str = "base"
    movement_flag: bool = False
    current_command: str | None = None
    current_role: Literal["commander", "follower"] = "follower"
    role_epoch: int = 0
    hierarchy_level: int = 0
    parent_id: str | None = None
    follower_ids: list[str] = field(default_factory=list)

    def __setattr__(self, name: str, value: object) -> None:
        """Validate modeled fields whenever callers assign them."""
        if name in self.__dataclass_fields__:
            self._validate_field(name, value)
            if name in {"capabilities", "follower_ids"}:
                value = value.copy()
        object.__setattr__(self, name, value)

    @staticmethod
    def _validate_field(name: str, value: object) -> None:
        if name in {"battery_percent", "role_epoch", "hierarchy_level"}:
            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")
            if name == "battery_percent" and not 0 <= value <= 100:
                raise ValueError("battery_percent must be between 0 and 100")
            if name != "battery_percent" and value < 0:
                raise ValueError(f"{name} must be non-negative")
        elif name in {
            "obstacle_detected",
            "mechanical_fault",
            "movement_flag",
        }:
            if type(value) is not bool:
                raise TypeError(f"{name} must be a boolean")
        elif name in {"terrain", "position"}:
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")
        elif name in {"capabilities", "follower_ids"}:
            if not isinstance(value, list):
                raise TypeError(f"{name} must be a list of strings")
            if any(not isinstance(item, str) for item in value):
                raise TypeError(f"{name} must be a list of strings")
        elif name == "current_command":
            if value is not None and not isinstance(value, str):
                raise TypeError("current_command must be a string or None")
        elif name == "current_role":
            if not isinstance(value, str):
                raise TypeError("current_role must be a string")
            if value not in {"commander", "follower"}:
                raise ValueError("current_role must be 'commander' or 'follower'")
        elif name == "parent_id":
            if value is not None and not isinstance(value, str):
                raise TypeError("parent_id must be a string or None")

    def force_state(self, overrides: Mapping[str, object]) -> None:
        """Apply validated test overrides atomically."""
        if not isinstance(overrides, Mapping):
            raise TypeError("overrides must be a mapping")

        for name, value in overrides.items():
            if name not in self.__dataclass_fields__:
                raise ValueError(f"unknown follower state field: {name}")
            self._validate_field(name, value)

        for name, value in overrides.items():
            setattr(self, name, value)


def load_scenario(path: str | Path) -> FollowerState:
    """Load state overrides from a YAML scenario file."""
    scenario_path = Path(path)
    with scenario_path.open(encoding="utf-8") as scenario_file:
        overrides = yaml.safe_load(scenario_file)

    if not isinstance(overrides, dict):
        raise ValueError("scenario YAML must contain a mapping of state overrides")

    state = FollowerState()
    state.force_state(overrides)
    return state
