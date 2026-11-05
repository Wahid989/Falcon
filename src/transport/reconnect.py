"""Explicit bounded connection recovery; never retries a command or decides roles."""

import logging
from collections.abc import Callable
from threading import Event, Lock

from .base import (
    Message,
    Transport,
    TransportClosedError,
    TransportConnectionError,
    TransportNotConnectedError,
    TransportTimeoutError,
    TransportUnreachableError,
)
from .tcp import _timeout

_LOGGER = logging.getLogger(__name__)


class ReconnectingTransport(Transport):
    """Wrap fresh endpoint factories; call reconnect() after a lost connection.

    on_connect runs on every established link, before it is returned to callers.
    It is the application's bounded registration/handshake hook, not a new wire type.
    """

    def __init__(
        self,
        factory: Callable[[], Transport],
        *,
        max_retries: int = 5,
        initial_backoff: float = 1.0,
        max_backoff: float = 30.0,
        on_connect: Callable[[Transport], None] | None = None,
    ):
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        self._initial_backoff = _timeout(initial_backoff)
        self._max_backoff = _timeout(max_backoff)
        if self._initial_backoff > self._max_backoff:
            raise ValueError("Initial backoff must not exceed maximum backoff")
        self._factory = factory
        self._max_retries = max_retries
        self._on_connect = on_connect
        self._closed = Event()
        self._lock = Lock()
        self._connect_lock = Lock()
        self._link: Transport | None = None
        self._ready = False
        self._unreachable = False

    @property
    def unreachable(self) -> bool:
        """True after retry exhaustion; cleared by a successful explicit recovery."""
        with self._lock:
            return self._unreachable

    def _check_closed(self):
        if self._closed.is_set():
            raise TransportClosedError("Endpoint is closed")

    def _connection(self):
        with self._lock:
            self._check_closed()
            if self._unreachable:
                raise TransportUnreachableError("Peer is unreachable")
            if not self._ready:
                raise TransportNotConnectedError("Endpoint is not connected")
            return self._link

    def connect(self) -> None:
        with self._connect_lock:
            self._check_closed()
            with self._lock:
                if self._ready:
                    return
            self._establish()

    def reconnect(self) -> None:
        """Discard the old connection and run a new bounded connection window."""
        with self._connect_lock:
            self._check_closed()
            with self._lock:
                self._ready = False
                previous, self._link = self._link, None
            if previous is not None:
                previous.close()
            self._establish()

    def _establish(self):
        delay = self._initial_backoff
        last_error = None
        for attempt in range(self._max_retries + 1):
            self._check_closed()
            link = None
            try:
                link = self._factory()
                with self._lock:
                    self._check_closed()
                    self._link = link
                link.connect()
                if self._on_connect is not None:
                    self._on_connect(link)
                with self._lock:
                    self._check_closed()
                    self._ready = True
                    self._unreachable = False
                _LOGGER.info("Connection established on attempt %d", attempt + 1)
                return
            except (
                TransportConnectionError,
                TransportTimeoutError,
                TransportClosedError,
            ) as exc:
                if link is not None:
                    link.close()
                self._check_closed()
                last_error = exc
            except BaseException:
                if link is not None:
                    link.close()
                raise
            if attempt < self._max_retries:
                _LOGGER.warning(
                    "Connection attempt %d failed; retrying in %.3f seconds",
                    attempt + 1,
                    delay,
                )
                if self._closed.wait(delay):
                    raise TransportClosedError("Endpoint is closed")
                delay = min(delay * 2, self._max_backoff)
        with self._lock:
            self._unreachable = True
        _LOGGER.error("Peer unreachable after %d attempts", self._max_retries + 1)
        raise TransportUnreachableError("Connection retries exhausted") from last_error

    def send(self, message: Message) -> None:
        self._connection().send(message)

    def receive(self, timeout: float) -> Message:
        return self._connection().receive(timeout)

    def close(self) -> None:
        with self._lock:
            self._closed.set()
            self._ready = False
            if self._link is not None:
                self._link.close()
