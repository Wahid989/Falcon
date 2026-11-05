"""Socket-level regressions for buffering, deadlines, bad input, and shutdown."""

from concurrent.futures import ThreadPoolExecutor
import socket
import struct
from threading import Event
from time import monotonic

import pytest

from src.transport import (
    TCPClientTransport,
    TCPServerTransport,
    TransportClosedError,
    TransportConnectionError,
    TransportMessageError,
    TransportNotConnectedError,
    TransportTimeoutError,
)
from src.transport.framing import encode_frame


@pytest.fixture
def raw_peer():
    server = TCPServerTransport(connect_timeout=0.5, io_timeout=0.02)
    peer = socket.create_connection(server.listen(), timeout=1)
    server.connect()
    try:
        yield server, peer
    finally:
        server.close()
        peer.close()


@pytest.mark.parametrize("boundary", [1, 3, 4, 8])
def test_partial_frame_survives_timeout(raw_peer, boundary):
    server, peer = raw_peer
    frame = encode_frame({"payload": {"unicode": "🦅"}})
    peer.sendall(frame[:boundary])
    with pytest.raises(TransportTimeoutError):
        server.receive(0.01)
    peer.sendall(frame[boundary:] + encode_frame({"second": True}))
    assert server.receive(1) == {"payload": {"unicode": "🦅"}}
    assert server.receive(1) == {"second": True}


def test_many_messages_fifo_and_independent_copies(raw_peer):
    server, peer = raw_peer
    peer.sendall(b"".join(encode_frame({"n": n}) for n in range(50)))
    assert [server.receive(1) for _ in range(50)] == [{"n": n} for n in range(50)]


@pytest.mark.parametrize(
    "wire",
    [
        struct.pack("!I", 0),
        struct.pack("!I", 1048577),
        struct.pack("!I", 1) + b"\xff",
        struct.pack("!I", 1) + b"{",
        struct.pack("!I", 2) + b"[]",
    ],
)
def test_hostile_frames_close_link(raw_peer, wire):
    server, peer = raw_peer
    peer.sendall(wire)
    with pytest.raises(TransportMessageError):
        server.receive(1)
    with pytest.raises(TransportClosedError):
        server.receive(0)


@pytest.mark.parametrize("partial", [b"\x00", struct.pack("!I", 10) + b"{}"])
def test_truncated_frame(raw_peer, partial):
    server, peer = raw_peer
    peer.sendall(partial)
    peer.shutdown(socket.SHUT_WR)
    with pytest.raises(TransportMessageError):
        server.receive(1)


def test_drain_before_peer_eof(raw_peer):
    server, peer = raw_peer
    peer.sendall(encode_frame({"first": True}) + encode_frame({"last": True}))
    peer.shutdown(socket.SHUT_WR)
    assert server.receive(1) == {"first": True}
    assert server.receive(1) == {"last": True}
    with pytest.raises(TransportClosedError):
        server.receive(1)


def test_bad_send_does_not_poison_link(raw_peer):
    server, peer = raw_peer
    with pytest.raises(TransportMessageError):
        server.send({"unsupported": object()})
    peer.sendall(encode_frame({"still": "connected"}))
    assert server.receive(1) == {"still": "connected"}


def test_send_backpressure_timeout_closes_link(raw_peer):
    server, peer = raw_peer
    server._socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
    peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
    # No peer reads: a frame bigger than the socket buffers cannot finish.
    with pytest.raises(TransportTimeoutError):
        server.send({"data": "x" * 900000})
    with pytest.raises(TransportClosedError):
        server.send({})


@pytest.mark.parametrize("operation", ["accept", "receive"])
def test_close_wakes_waiters(operation):
    server = TCPServerTransport(connect_timeout=5)
    client = None
    entered = Event()
    original_wait = server._wait

    def observed_wait(*args, **kwargs):
        entered.set()
        return original_wait(*args, **kwargs)

    server._wait = observed_wait
    try:
        server.listen()
        if operation == "receive":
            client = TCPClientTransport(*server.listen())
            client.connect()
            server.connect()
            entered.clear()
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(
                server.connect if operation == "accept" else lambda: server.receive(5)
            )
            assert entered.wait(1)
            server.close()
            with pytest.raises(TransportClosedError):
                result.result(timeout=1)
    finally:
        server.close()
        if client is not None:
            client.close()


def test_lifecycle_and_address_in_use():
    server = TCPServerTransport(connect_timeout=0.01)
    other = None
    try:
        address = server.listen()
        assert server.listen() == address
        other = TCPServerTransport(*address)
        with pytest.raises(TransportConnectionError):
            other.listen()
        with pytest.raises(TransportNotConnectedError):
            server.send({})
        with pytest.raises(TransportNotConnectedError):
            server.receive(0)
        server.close()
        server.close()
        with pytest.raises(TransportClosedError):
            server.connect()
    finally:
        server.close()
        if other is not None:
            other.close()


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan"), True, None])
def test_invalid_receive_timeout(raw_peer, timeout):
    with pytest.raises(ValueError):
        raw_peer[0].receive(timeout)


def test_receive_deadline_is_bounded(raw_peer):
    server, peer = raw_peer
    peer.sendall(b"\x00")
    start = monotonic()
    with pytest.raises(TransportTimeoutError):
        server.receive(0.03)
    assert 0.02 <= monotonic() - start < 1
