"""Commander module: the command-issuing side of an agent.

Builds FALCON v1 command envelopes, validates them before they leave, sends
them over one transport link per follower, and matches each follower's
response to its command through ``payload.in_reply_to``.

The module holds no transport of its own; links are created by the runtime
and handed in with ``add_follower``. Message validation is injected the same
way, as a ``validate(message) -> (ok, errors)`` callable.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from time import monotonic
from typing import Callable

from src.transport import Message, Transport, TransportError, TransportTimeoutError

Validator = Callable[[Message], tuple[bool, list[str]]]

SCHEMA_VERSION = 1
COMMANDS = ("HOLD", "PROCEED_TO_WAYPOINT", "RETURN", "REPORT_STATUS")

# How many unsolicited or rejected messages are kept per follower. Older ones
# are dropped so a chatty or misbehaving peer cannot grow memory without bound.
KEEP_PER_FOLLOWER = 256


class CommanderError(Exception):
    """A command could not be built or sent."""


class InvalidCommandError(CommanderError):
    """The command failed validation; nothing was sent."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("Command failed validation: " + "; ".join(errors))
        self.errors = errors


class UnknownFollowerError(CommanderError):
    """No link is registered for that follower."""


class Outcome(str, Enum):
    """How a command ended, from the commander's point of view."""

    COMPLY = "COMPLY"
    DECLINE = "DECLINE"
    STATUS_RESPONSE = "STATUS_RESPONSE"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    INVALID_RESPONSE = "INVALID_RESPONSE"


RESPONSE_OUTCOMES = {
    outcome.value: outcome
    for outcome in (
        Outcome.COMPLY,
        Outcome.DECLINE,
        Outcome.STATUS_RESPONSE,
        Outcome.ERROR,
    )
}


@dataclass(frozen=True)
class CommandResult:
    """A sent command and what came back for it."""

    command: Message
    outcome: Outcome
    response: Message | None = None
    problems: tuple[str, ...] = ()

    def _payload_field(self, name: str):
        if self.response is None or not isinstance(self.response.get("payload"), dict):
            return None
        return self.response["payload"].get(name)

    @property
    def reason(self) -> str | None:
        """Decline reason, when the follower declined."""
        return self._payload_field("reason")

    @property
    def state(self) -> dict | None:
        """Reported follower state, for a STATUS_RESPONSE."""
        return self._payload_field("state")

    @property
    def error(self) -> dict | None:
        """Protocol error detail, when the follower returned ERROR."""
        return self._payload_field("error")


@dataclass(frozen=True)
class BroadcastResult:
    """Commands sent by a broadcast, and the followers it could not reach."""

    sent: dict[str, Message]
    failed: dict[str, TransportError]


@dataclass
class _Link:
    transport: Transport
    # Commands sent on this link that have not been settled yet.
    pending: set[str] = field(default_factory=set)
    # Valid responses to pending commands, waiting to be collected.
    responses: dict[str, Message] = field(default_factory=dict)
    # Non-response messages (e.g. heartbeats) and responses nobody is waiting
    # for (e.g. one that arrived after its command timed out).
    other: deque[Message] = field(
        default_factory=lambda: deque(maxlen=KEEP_PER_FOLLOWER)
    )
    # Responses that failed validation, with the problems found.
    rejected: deque[tuple[Message, list[str]]] = field(
        default_factory=lambda: deque(maxlen=KEEP_PER_FOLLOWER)
    )


def default_validator() -> Validator:
    """The project's message validator (src.schema.validator.validate)."""
    try:
        from src.schema.validator import validate
    except ImportError as exc:
        raise CommanderError(
            "No message validator is available; pass one to Commander(validator=...)"
        ) from exc
    return validate


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _new_message_id() -> str:
    return str(uuid.uuid4())


class Commander:
    """Issues commands to followers and reads their responses.

    Not thread-safe: use one Commander from one thread, as the transport docs
    recommend one receive loop per link.
    """

    def __init__(
        self,
        agent_id: str,
        *,
        role_epoch: int = 0,
        validator: Validator | None = None,
        clock: Callable[[], str] = _utc_now,
        new_id: Callable[[], str] = _new_message_id,
    ) -> None:
        self.agent_id = agent_id
        self.role_epoch = role_epoch
        self._validate = validator if validator is not None else default_validator()
        self._clock = clock
        self._new_id = new_id
        self._links: dict[str, _Link] = {}

    # --- followers -----------------------------------------------------------

    def add_follower(self, follower_id: str, transport: Transport) -> None:
        """Register the link to a direct follower. The caller owns its lifetime."""
        if follower_id in self._links:
            raise CommanderError(f"Follower {follower_id!r} is already registered")
        self._links[follower_id] = _Link(transport)

    def remove_follower(self, follower_id: str) -> None:
        self._link(follower_id)
        del self._links[follower_id]

    @property
    def followers(self) -> list[str]:
        return list(self._links)

    # --- building and sending ------------------------------------------------

    def build_command(
        self, recipient_id: str, command: str, *, waypoint: str | None = None
    ) -> Message:
        """Return a validated command envelope. Raises InvalidCommandError."""
        payload: dict = {"command": command}
        if waypoint is not None:
            payload["waypoint"] = waypoint
        message: Message = {
            "message_id": self._new_id(),
            "sender_id": self.agent_id,
            "recipient_id": recipient_id,
            "timestamp": self._clock(),
            "schema_version": SCHEMA_VERSION,
            "sender_role": "commander",
            "role_epoch": self.role_epoch,
            "type": "command",
            "payload": payload,
        }
        ok, errors = self._validate(message)
        if not ok:
            raise InvalidCommandError(list(errors) or ["message is invalid"])
        return message

    def send_command(
        self, follower_id: str, command: str, *, waypoint: str | None = None
    ) -> Message:
        """Build, validate and send a command; returns the message sent."""
        link = self._link(follower_id)
        message = self.build_command(follower_id, command, waypoint=waypoint)
        self._send(link, message)
        return message

    def broadcast(
        self, command: str, *, waypoint: str | None = None
    ) -> BroadcastResult:
        """Send the same command to every direct follower, one message each.

        Every message is built and validated before any is sent. A follower
        whose link fails does not stop the others; it is listed in ``failed``.
        """
        messages = {
            follower_id: self.build_command(follower_id, command, waypoint=waypoint)
            for follower_id in self._links
        }
        sent: dict[str, Message] = {}
        failed: dict[str, TransportError] = {}
        for follower_id, message in messages.items():
            try:
                self._send(self._links[follower_id], message)
            except TransportError as error:
                failed[follower_id] = error
            else:
                sent[follower_id] = message
        return BroadcastResult(sent, failed)

    # --- responses -----------------------------------------------------------

    def await_response(self, command: Message, timeout: float) -> CommandResult:
        """Wait up to ``timeout`` seconds for the response to ``command``.

        Messages that arrive in the meantime are filed, not discarded. A
        timeout of 0 still checks what has already arrived. Transport failures
        other than a timeout (for example a closed link) are raised.
        """
        link = self._link(command["recipient_id"])
        message_id = command["message_id"]
        deadline = monotonic() + timeout

        while True:
            if message_id in link.responses:
                link.pending.discard(message_id)
                return self._result(command, link.responses.pop(message_id))
            remaining = deadline - monotonic()
            try:
                message = link.transport.receive(timeout=max(remaining, 0.0))
            except TransportTimeoutError:
                return self._timed_out(link, command)
            settled = self._accept(link, command, message)
            if settled is not None:
                link.pending.discard(message_id)
                return settled
            if remaining <= 0 and message_id not in link.responses:
                return self._timed_out(link, command)

    def issue(
        self,
        follower_id: str,
        command: str,
        *,
        waypoint: str | None = None,
        timeout: float,
    ) -> CommandResult:
        """Send a command and wait for its response."""
        message = self.send_command(follower_id, command, waypoint=waypoint)
        return self.await_response(message, timeout)

    def other_messages(self, follower_id: str) -> list[Message]:
        """Remove and return messages from a follower that no command claimed."""
        link = self._link(follower_id)
        drained = list(link.other)
        link.other.clear()
        return drained

    def rejected_responses(self, follower_id: str) -> list[tuple[Message, list[str]]]:
        """Responses from a follower that failed validation, with the reasons."""
        return list(self._link(follower_id).rejected)

    # --- internals -----------------------------------------------------------

    def _link(self, follower_id: str) -> _Link:
        try:
            return self._links[follower_id]
        except KeyError:
            raise UnknownFollowerError(f"No link to follower {follower_id!r}") from None

    @staticmethod
    def _send(link: _Link, message: Message) -> None:
        link.transport.send(message)
        link.pending.add(message["message_id"])

    @staticmethod
    def _timed_out(link: _Link, command: Message) -> CommandResult:
        # A response that turns up later is treated as unsolicited.
        link.pending.discard(command["message_id"])
        return CommandResult(command, Outcome.TIMEOUT)

    def _accept(
        self, link: _Link, command: Message, message: Message
    ) -> CommandResult | None:
        """File one received message; return a result if it settles ``command``."""
        if message.get("type") != "response":
            link.other.append(message)
            return None

        payload = message.get("payload")
        in_reply_to = payload.get("in_reply_to") if isinstance(payload, dict) else None
        answers_command = in_reply_to == command["message_id"]
        problems = self._response_problems(command["recipient_id"], message)

        if problems:
            link.rejected.append((message, problems))
            if answers_command:
                return CommandResult(
                    command, Outcome.INVALID_RESPONSE, message, tuple(problems)
                )
            return None

        if isinstance(in_reply_to, str) and in_reply_to in link.pending:
            link.responses[in_reply_to] = message
        else:
            link.other.append(message)
        return None

    def _response_problems(self, follower_id: str, message: Message) -> list[str]:
        try:
            ok, errors = self._validate(message)
        except Exception as error:  # a received message must never crash us
            ok, errors = False, [f"validator failed: {error}"]
        problems = [] if ok else list(errors) or ["message is invalid"]

        payload = message.get("payload")
        if not isinstance(payload, dict):
            problems.append("payload is not an object")
        elif payload.get("response") not in RESPONSE_OUTCOMES:
            problems.append(f"unknown response {payload.get('response')!r}")
        elif not isinstance(payload.get("in_reply_to"), str):
            problems.append("in_reply_to is not a string")

        if message.get("sender_id") != follower_id:
            problems.append(
                f"sender_id is {message.get('sender_id')!r}, expected {follower_id!r}"
            )
        if message.get("recipient_id") != self.agent_id:
            problems.append(
                f"recipient_id is {message.get('recipient_id')!r},"
                f" expected {self.agent_id!r}"
            )
        return problems

    @staticmethod
    def _result(command: Message, response: Message) -> CommandResult:
        return CommandResult(
            command, RESPONSE_OUTCOMES[response["payload"]["response"]], response
        )
