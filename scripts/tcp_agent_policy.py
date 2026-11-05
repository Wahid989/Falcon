"""Small injected demo policy, not the complete Phase-4 decision engine.

Only movement battery/mechanical/obstacle guards are demonstrated. No terrain,
capability, busy, or role eligibility policy is claimed here.
"""

from src.decision import DecisionResult, DeclineReason


def decide(state, command, payload, message_role_epoch):
    if command in {"PROCEED_TO_WAYPOINT", "RETURN"}:
        if state.battery_percent < 20:
            return DecisionResult(False, DeclineReason.BATTERY_CRITICAL)
        if state.mechanical_fault:
            return DecisionResult(False, DeclineReason.MECHANICAL_FAULT)
        if command == "PROCEED_TO_WAYPOINT" and state.obstacle_detected:
            return DecisionResult(False, DeclineReason.OBSTACLE_DETECTED)
    return DecisionResult(True)
