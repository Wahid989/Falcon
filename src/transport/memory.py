"""Thread-safe paired transport for tests, with no sockets or agent policy."""

from collections import deque
from math import isfinite
from threading import Condition, TIMEOUT_MAX
from time import monotonic

from .base import (
    JSONValue,
    Message,
    Transport,
    TransportClosedError,
    TransportMessageError,
    TransportNotConnectedError,
    TransportTimeoutError,
)


def _copy_json(value: JSONValue, ancestors: set[int]) -> JSONValue:
    """Copy strict JSON values without coercing keys, tuples, or nonfinite floats."""
    if type(value) in (str, int, bool, type(None)):
        return value
    if type(value) is float and isfinite(value):
        return value
    if type(value) not in (dict, list):
        raise TransportMessageError("Message contains a non-JSON value")
    identity = id(value)
    if identity in ancestors:
        raise TransportMessageError("Message contains a circular reference")
    ancestors.add(identity)
    try:
        if type(value) is list:
            return [_copy_json(item, ancestors) for item in value]
        if any(type(key) is not str for key in value):
            raise TransportMessageError("JSON object keys must be strings")
        return {key: _copy_json(item, ancestors) for key, item in value.items()}
    finally:
        ancestors.remove(identity)


class InMemoryTransport(Transport):
    """Create endpoints with pair(); each endpoint has one FIFO inbox."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._peer: InMemoryTransport | None = None
        self._connected = False
        self._closed = False
        self._inbox: deque[Message] = deque()

    @classmethod
    def pair(cls) -> tuple["InMemoryTransport", "InMemoryTransport"]:
        """Return two initially disconnected endpoints sharing a lock."""
        left, right = cls(), cls()
        right._condition = left._condition
        left._peer, right._peer = right, left
        return left, right

    def _check_open(self) -> None:
        if self._closed:
            raise TransportClosedError("Endpoint is closed")
        if not self._connected:
            raise TransportNotConnectedError("Endpoint is not connected")

    def connect(self) -> None:
        with self._condition:
            if self._closed or (self._peer is not None and self._peer._closed):
                raise TransportClosedError("Link is closed")
            if self._peer is None:
                raise TransportNotConnectedError("Create endpoints with pair()")
            self._connected = True

    def send(self, message: Message) -> None:
        with self._condition:
            self._check_open()
            peer = self._peer
            if peer._closed:
                raise TransportClosedError("Peer is closed")
            if not peer._connected:
                raise TransportNotConnectedError("Peer is not connected")
            if type(message) is not dict:
                raise TransportMessageError("Message must be a JSON object")
            try:
                snapshot = _copy_json(message, set())
            except RecursionError as exc:
                raise TransportMessageError("Message nesting is too deep") from exc
            peer._inbox.append(snapshot)
            self._condition.notify_all()

    def receive(self, timeout: float) -> Message:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("Timeout must be finite non-negative seconds")
        try:
            valid = 0 <= timeout <= TIMEOUT_MAX and isfinite(timeout)
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("Timeout must be finite and between 0 and TIMEOUT_MAX")
        deadline = monotonic() + timeout
        with self._condition:
            while True:
                self._check_open()
                if self._inbox:
                    return self._inbox.popleft()
                if self._peer._closed:
                    raise TransportClosedError("Peer is closed")
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TransportTimeoutError("Receive timed out")
                self._condition.wait(remaining)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._inbox.clear()
            self._condition.notify_all()
