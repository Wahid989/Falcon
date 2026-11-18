"""Follower command receiver using injected transport, validation, and policy."""

from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from re import fullmatch
from typing import Callable

from src.decision import DecisionEngine, DecisionResult, decide
from src.state import (
    CommandLifecycle,
    FollowerState,
    apply_command_transition,
)
from src.transport import JSONValue, Message, Transport

Validator = Callable[[Message], tuple[bool, list[str]]]

SCHEMA_VERSION = 1


class FollowerError(Exception):
    """Base class for follower processing failures."""


class InvalidFollowerMessageError(FollowerError):
    """A message cannot be safely handled or answered."""


class InvalidFollowerResponseError(FollowerError):
    """The constructed response failed protocol validation."""


def default_validator() -> Validator:
    """Load the project schema validator when FALCON-019 is available."""
    try:
        from src.schema.validator import validate
    except ImportError as exc:
        raise FollowerError(
            "No message validator is available; pass one to Follower(validator=...)"
        ) from exc
    return validate


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _new_message_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class FollowerResult:
    """A processed command, its decision, transition, and sent response."""

    request: Message
    decision: DecisionResult | None
    lifecycle: tuple[CommandLifecycle, ...] | None
    response: Message


class Follower:
    """Receive one command and return a correlated response.

    The validator and decision engine can be injected. By default, the
    production schema validator and FALCON-031 decision policy are used.
    """

    def __init__(
        self,
        agent_id: str,
        transport: Transport,
        decision_engine: DecisionEngine | None = None,
        state: FollowerState | None = None,
        validator: Validator | None = None,
        exchange_log=None,
    ) -> None:
        if (
            not isinstance(agent_id, str)
            or fullmatch(r"[A-Za-z0-9_-]+", agent_id) is None
        ):
            raise ValueError("agent_id must contain only letters, digits, '_' or '-'")
        if not isinstance(transport, Transport):
            raise TypeError("transport must implement Transport")
        if decision_engine is not None and not callable(decision_engine):
            raise TypeError("decision_engine must be callable")

        self._exchange_log = exchange_log
        self.agent_id = agent_id
        self.transport = transport
        self.state = state if state is not None else FollowerState()
        if not isinstance(self.state, FollowerState):
            raise TypeError("state must be a FollowerState")
        self._decision_engine = (
            decision_engine if decision_engine is not None else decide
        )
        self._validate = validator if validator is not None else default_validator()

    def handle_once(self, timeout: float) -> FollowerResult | None:
        """Receive and process one command; ignore non-command messages."""
        message = self.transport.receive(timeout)
        if self._exchange_log is not None:
            self._exchange_log.record("received", message)
        valid, problems = self._validate(message)
        if not valid:
            if self._exchange_log is not None:
                self._exchange_log.event(
                    "INVALID_MESSAGE",
                    {
                        "message_id": message.get("message_id"),
                        "sender_id": message.get("sender_id"),
                        "problems": problems,
                    },
                )
            return self._send_error_for_invalid_message(message, problems)
        if message["recipient_id"] != self.agent_id:
            return self._send_error(
                message,
                "INVALID_SENDER_OR_RECIPIENT",
                "message is addressed to another agent",
            )
        if (
            message["sender_role"] != "commander"
            or self.state.parent_id is None
            or message["sender_id"] != self.state.parent_id
        ):
            return self._send_error(
                message, "UNAUTHORIZED", "sender is not the configured commander"
            )

        if message["role_epoch"] < self.state.role_epoch:
            return self._send_error(
                message,
                "STALE_EPOCH",
                "command role_epoch is older than the follower's current epoch",
            )

        if message["type"] != "command":
            return None

        payload = message["payload"]
        if not isinstance(payload, dict):
            return self._send_error(
                message, "MISSING_FIELD", "payload must be an object"
            )

        command = payload.get("command")
        if not isinstance(command, str):
            return self._send_error(
                message, "UNSUPPORTED_COMMAND", "command is missing or invalid"
            )
        command_payload = {
            key: value for key, value in payload.items() if key != "command"
        }
        role_epoch = message["role_epoch"]
        if type(role_epoch) is not int:
            return self._send_error(message, "MISSING_FIELD", "role_epoch is invalid")

        decision = self._decision_engine(
            deepcopy(self.state),
            command,
            command_payload,
            role_epoch,
        )
        if not isinstance(decision, DecisionResult):
            raise TypeError("decision_engine must return a DecisionResult")

        transition = apply_command_transition(
            self.state,
            command,
            command_payload,
            complied=decision.complied,
        )
        self.state = transition.state
        response = self._build_command_response(message, command, decision)
        self._send_validated(response)
        return FollowerResult(
            request=message,
            decision=decision,
            lifecycle=transition.lifecycle,
            response=response,
        )

    def _send_error_for_invalid_message(
        self, message: Message, problems: list[str]
    ) -> FollowerResult:
        if not isinstance(message.get("message_id"), str) or not message["message_id"]:
            raise InvalidFollowerMessageError(
                "cannot send a correlated error response without message_id"
            )
        if not isinstance(message.get("sender_id"), str) or not message["sender_id"]:
            raise InvalidFollowerMessageError(
                "cannot send a correlated error response without sender_id"
            )
        if fullmatch(r"[A-Za-z0-9_-]+", message["sender_id"]) is None:
            raise InvalidFollowerMessageError(
                "cannot send a correlated error response to an invalid sender_id"
            )
        code = self._validation_error_code(message)
        response = self._error_response(
            message,
            code,
            "; ".join(problems) or "message failed schema validation",
        )
        self._send_validated(response)
        return FollowerResult(
            request=message,
            decision=None,
            lifecycle=None,
            response=response,
        )

    @staticmethod
    def _validation_error_code(message: Message) -> str:
        message_type = message.get("type")
        if isinstance(message_type, str) and message_type not in {
            "command",
            "response",
            "role_request",
            "role_grant",
            "role_deny",
            "heartbeat",
        }:
            return "UNKNOWN_TYPE"
        payload = message.get("payload")
        if isinstance(payload, dict):
            command = payload.get("command")
            if isinstance(command, str) and command not in {
                "HOLD",
                "PROCEED_TO_WAYPOINT",
                "RETURN",
                "REPORT_STATUS",
            }:
                return "UNSUPPORTED_COMMAND"
        sender_id = message.get("sender_id")
        recipient_id = message.get("recipient_id")
        if (
            not isinstance(sender_id, str)
            or fullmatch(r"[A-Za-z0-9_-]+", sender_id) is None
            or not isinstance(recipient_id, str)
            or fullmatch(r"[A-Za-z0-9_-]+", recipient_id) is None
        ):
            return "INVALID_SENDER_OR_RECIPIENT"
        return "MISSING_FIELD"

    def _send_error(self, message: Message, code: str, detail: str) -> FollowerResult:
        response = self._error_response(message, code, detail)
        self._send_validated(response)
        return FollowerResult(
            request=message,
            decision=None,
            lifecycle=None,
            response=response,
        )

    def _error_response(self, message: Message, code: str, detail: str) -> Message:
        message_id = message.get("message_id")
        sender_id = message.get("sender_id")
        if not isinstance(message_id, str) or not message_id:
            raise InvalidFollowerMessageError(
                "cannot send a correlated error response without message_id"
            )
        if not isinstance(sender_id, str) or not sender_id:
            raise InvalidFollowerMessageError(
                "cannot send a correlated error response without sender_id"
            )
        return self._envelope(
            sender_id,
            {
                "response": "ERROR",
                "in_reply_to": message_id,
                "error": {"code": code, "message": detail},
            },
        )

    def _build_command_response(
        self,
        message: Message,
        command: str,
        decision: DecisionResult,
    ) -> Message:
        message_id = message["message_id"]
        sender_id = message["sender_id"]
        if not isinstance(message_id, str) or not isinstance(sender_id, str):
            raise InvalidFollowerMessageError(
                "validated command has invalid correlation fields"
            )

        if not decision.complied:
            if decision.reason is None:
                raise ValueError("declined decision is missing its reason")
            response_payload = {
                "response": "DECLINE",
                "in_reply_to": message_id,
                "reason": decision.reason.value,
            }
        elif command == "REPORT_STATUS":
            response_payload = {
                "response": "STATUS_RESPONSE",
                "in_reply_to": message_id,
                "state": self.status_snapshot(),
            }
        else:
            response_payload = {
                "response": "COMPLY",
                "in_reply_to": message_id,
            }
        return self._envelope(sender_id, response_payload)

    def _envelope(self, recipient_id: str, payload: dict[str, JSONValue]) -> Message:
        return {
            "message_id": _new_message_id(),
            "sender_id": self.agent_id,
            "recipient_id": recipient_id,
            "timestamp": _utc_now(),
            "schema_version": SCHEMA_VERSION,
            "sender_role": "follower",
            "role_epoch": self.state.role_epoch,
            "type": "response",
            "payload": payload,
        }

    def _send_validated(self, message: Message) -> None:
        valid, errors = self._validate(message)
        if not valid:
            if self._exchange_log is not None:
                self._exchange_log.event(
                    "INVALID_OUTGOING_RESPONSE",
                    {"problems": errors, "message_id": message.get("message_id")},
                )
            raise InvalidFollowerResponseError(
                "constructed response failed validation: " + "; ".join(errors)
            )
        self.transport.send(message)
        if self._exchange_log is not None:
            self._exchange_log.record("sent", message)
            if message.get("payload", {}).get("response") == "ERROR":
                self._exchange_log.event(
                    "PROTOCOL_ERROR",
                    {
                        "message_id": message["message_id"],
                        "in_reply_to": message["payload"]["in_reply_to"],
                        "recipient_id": message["recipient_id"],
                        "error": message["payload"]["error"],
                    },
                )

    def status_snapshot(self) -> dict[str, JSONValue]:
        """Return wire-safe state without the log-only hierarchy level."""
        return {
            "agent_id": self.agent_id,
            "battery_percent": self.state.battery_percent,
            "obstacle_detected": self.state.obstacle_detected,
            "mechanical_fault": self.state.mechanical_fault,
            "terrain": self.state.terrain,
            "capabilities": self.state.capabilities.copy(),
            "position": self.state.position,
            "movement_flag": self.state.movement_flag,
            "current_command": self.state.current_command,
            "current_role": self.state.current_role,
            "role_epoch": self.state.role_epoch,
        }
