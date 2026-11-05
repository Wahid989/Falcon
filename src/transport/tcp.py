"""Single-peer synchronous TCP endpoints with bounded I/O and terminal close."""

import errno
import ipaddress
import select
import socket
from math import isfinite
from threading import Event, Lock, TIMEOUT_MAX
from time import monotonic

from .base import (
    Message,
    Transport,
    TransportClosedError,
    TransportConnectionError,
    TransportMessageError,
    TransportNotConnectedError,
    TransportTimeoutError,
)
from .framing import DEFAULT_MAX_FRAME_BYTES, FrameBuffer, encode_frame


def _timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Timeout must be finite non-negative seconds")
    if not 0 <= value <= TIMEOUT_MAX or not isfinite(value):
        raise ValueError("Timeout is outside the supported finite range")
    return float(value)


def _address(host: str, port: int) -> tuple[str, int]:
    # Avoid an unbounded DNS lookup during connection setup.
    if host == "localhost":
        host = "127.0.0.1"
    try:
        ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError as exc:
        raise ValueError("Host must be an IPv4 literal or localhost") from exc
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Port must be between 0 and 65535")
    return host, port


class _TCPTransport(Transport):
    def __init__(self, *, io_timeout=5.0, max_frame_bytes=DEFAULT_MAX_FRAME_BYTES):
        self._io_timeout = _timeout(io_timeout)
        self._buffer = FrameBuffer(max_frame_bytes)
        self._closed = Event()
        self._state_lock = Lock()
        self._send_lock = Lock()
        self._receive_lock = Lock()
        self._connect_lock = Lock()
        self._socket: socket.socket | None = None
        self._listener: socket.socket | None = None
        self._pending: socket.socket | None = None

    def _check_closed(self):
        if self._closed.is_set():
            raise TransportClosedError("Endpoint is closed")

    def _connection(self):
        with self._state_lock:
            self._check_closed()
            if self._socket is None:
                raise TransportNotConnectedError("Endpoint is not connected")
            return self._socket

    def _install(self, connection):
        connection.setblocking(False)
        with self._state_lock:
            if self._closed.is_set():
                connection.close()
                raise TransportClosedError("Endpoint is closed")
            self._socket = connection

    def _wait(self, connection, *, reading, deadline):
        """Poll in bounded slices so close interrupts accept/read/write on all OSes."""
        while True:
            self._check_closed()
            remaining = max(0.0, deadline - monotonic())
            try:
                readable, writable, _ = select.select(
                    [connection] if reading else [],
                    [] if reading else [connection],
                    [],
                    min(remaining, 0.05),
                )
            except (OSError, ValueError) as exc:
                self.close()
                raise TransportClosedError("Connection is closed") from exc
            self._check_closed()
            if readable or writable:
                return
            if monotonic() >= deadline:
                raise TransportTimeoutError("Transport operation timed out")

    def send(self, message: Message) -> None:
        with self._send_lock:
            connection = self._connection()
            frame = encode_frame(message, self._buffer.max_bytes)
            deadline = monotonic() + self._io_timeout
            offset = 0
            try:
                while offset < len(frame):
                    self._wait(connection, reading=False, deadline=deadline)
                    try:
                        count = connection.send(frame[offset:])
                    except BlockingIOError:
                        continue
                    if count == 0:
                        raise OSError("Peer disconnected")
                    offset += count
                    if offset < len(frame) and monotonic() >= deadline:
                        raise TransportTimeoutError("Send timed out")
            except TransportTimeoutError:
                # An incomplete frame must never be followed by another send.
                self.close()
                raise
            except OSError as exc:
                self.close()
                raise TransportClosedError("Connection lost while sending") from exc

    def receive(self, timeout: float) -> Message:
        timeout = _timeout(timeout)
        deadline = monotonic() + timeout
        # Lock wait counts toward the receive deadline as well.
        if not self._receive_lock.acquire(timeout=timeout):
            raise TransportTimeoutError("Another receive is in progress")
        try:
            connection = self._connection()
            while True:
                try:
                    message = self._buffer.pop()
                    if message is not None:
                        return message
                    self._wait(connection, reading=True, deadline=deadline)
                    try:
                        chunk = connection.recv(65536)
                    except BlockingIOError:
                        chunk = None
                    if chunk == b"":
                        partial = bool(self._buffer.data)
                        self.close()
                        if partial:
                            raise TransportMessageError("Peer closed during a frame")
                        raise TransportClosedError("Peer disconnected")
                    if chunk:
                        self._buffer.feed(chunk)
                        message = self._buffer.pop()
                        if message is not None:
                            return message
                    if monotonic() >= deadline:
                        raise TransportTimeoutError("Receive timed out")
                except TransportMessageError:
                    self.close()
                    raise
                except OSError as exc:
                    self.close()
                    raise TransportClosedError(
                        "Connection lost while receiving"
                    ) from exc
        finally:
            self._receive_lock.release()

    def close(self) -> None:
        with self._state_lock:
            self._closed.set()
            for connection in (self._socket, self._listener, self._pending):
                if connection is not None:
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    connection.close()
            self._socket = self._listener = self._pending = None


class TCPServerTransport(_TCPTransport):
    """Listen for one peer. Call listen() before connect() to announce readiness."""

    def __init__(
        self,
        host="127.0.0.1",
        port=0,
        *,
        connect_timeout=5.0,
        io_timeout=5.0,
        max_frame_bytes=DEFAULT_MAX_FRAME_BYTES,
    ):
        super().__init__(io_timeout=io_timeout, max_frame_bytes=max_frame_bytes)
        self._address = _address(host, port)
        self._connect_timeout = _timeout(connect_timeout)

    def listen(self) -> tuple[str, int]:
        """Bind once; port zero chooses an available port. Does not accept yet."""
        with self._state_lock:
            self._check_closed()
            if self._socket is not None:
                return self._socket.getsockname()
            if self._listener is None:
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    listener.bind(self._address)
                    listener.listen(1)
                    listener.setblocking(False)
                except OSError as exc:
                    listener.close()
                    raise TransportConnectionError("Cannot listen on address") from exc
                self._listener = listener
            return self._listener.getsockname()

    def connect(self) -> None:
        with self._connect_lock:
            self._check_closed()
            if self._socket is not None:
                return
            self.listen()
            with self._state_lock:
                self._check_closed()
                listener = self._listener
            deadline = monotonic() + self._connect_timeout
            while True:
                self._wait(listener, reading=True, deadline=deadline)
                try:
                    connection, _ = listener.accept()
                    break
                except BlockingIOError:
                    if monotonic() >= deadline:
                        raise TransportTimeoutError("Accept timed out")
                except (OSError, AttributeError) as exc:
                    self._check_closed()
                    raise TransportConnectionError("Accept failed") from exc
            self._install(connection)
            with self._state_lock:
                listener.close()
                self._listener = None


class TCPClientTransport(_TCPTransport):
    """Connect to one IPv4 peer with bounded setup and no automatic replay."""

    def __init__(
        self,
        host,
        port,
        *,
        connect_timeout=5.0,
        io_timeout=5.0,
        max_frame_bytes=DEFAULT_MAX_FRAME_BYTES,
    ):
        super().__init__(io_timeout=io_timeout, max_frame_bytes=max_frame_bytes)
        self._address = _address(host, port)
        if port == 0:
            raise ValueError("Client port must be nonzero")
        self._connect_timeout = _timeout(connect_timeout)

    def connect(self) -> None:
        with self._connect_lock:
            self._check_closed()
            if self._socket is not None:
                return
            connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            connection.setblocking(False)
            try:
                with self._state_lock:
                    self._check_closed()
                    self._pending = connection
                error = connection.connect_ex(self._address)
                if error not in (0, errno.EISCONN):
                    if error not in (
                        errno.EINPROGRESS,
                        errno.EWOULDBLOCK,
                        errno.EALREADY,
                    ):
                        raise OSError(error, "Connection failed")
                    self._wait(
                        connection,
                        reading=False,
                        deadline=monotonic() + self._connect_timeout,
                    )
                    error = connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    if error:
                        raise OSError(error, "Connection failed")
                self._install(connection)
            except OSError as exc:
                self._check_closed()
                raise TransportConnectionError("Cannot connect to peer") from exc
            finally:
                with self._state_lock:
                    self._pending = None
                    if self._socket is not connection:
                        connection.close()
