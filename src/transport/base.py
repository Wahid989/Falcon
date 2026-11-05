"""Synchronous, message-oriented transport contract; no protocol policy."""

from abc import ABC, abstractmethod
from typing import TypeAlias

JSONValue: TypeAlias = (
    str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
)
Message: TypeAlias = dict[str, JSONValue]


class TransportError(Exception):
    """Base class for transport failures."""


class TransportConnectionError(TransportError):
    """Connection setup failed; callers may retry with a fresh endpoint."""


class TransportUnreachableError(TransportConnectionError):
    """The configured connection retry window was exhausted."""


class TransportNotConnectedError(TransportError):
    """The local endpoint or its peer has not connected yet."""


class TransportClosedError(TransportError):
    """The endpoint or link has been permanently closed."""


class TransportTimeoutError(TransportError):
    """A transport operation did not finish before its deadline."""


class TransportMessageError(TransportError):
    """The message is not a JSON object containing JSON-compatible values."""


class Transport(ABC):
    """One synchronous bidirectional link; see docs/transport.md."""

    @abstractmethod
    def connect(self) -> None:
        """Open the endpoint; repeated calls while connected are harmless."""

    @abstractmethod
    def send(self, message: Message) -> None:
        """Deliver an independent message snapshot, without awaiting a reply."""

    @abstractmethod
    def receive(self, timeout: float) -> Message:
        """Return the next message or raise; timeout is finite seconds >= 0."""

    @abstractmethod
    def close(self) -> None:
        """Permanently close the endpoint and wake waiting receivers."""
