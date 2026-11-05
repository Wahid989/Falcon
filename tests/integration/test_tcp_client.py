"""Commander-side client checks using real loopback sockets."""

import socket

import pytest

from src.transport import (
    TCPClientTransport,
    TCPServerTransport,
    TransportConnectionError,
    TransportTimeoutError,
)


def test_client_exchange_and_poll():
    server = TCPServerTransport()
    client = TCPClientTransport(*server.listen(), connect_timeout=1)
    try:
        client.connect()
        client.connect()
        server.connect()
        client.send({"command": "HOLD"})
        assert server.receive(1) == {"command": "HOLD"}
        server.send({"response": "COMPLY"})
        assert client.receive(1) == {"response": "COMPLY"}
        with pytest.raises(TransportTimeoutError):
            client.receive(0)
    finally:
        client.close()
        server.close()


def test_refused_connection():
    # Reserve an unlistened port so no unrelated service can claim it.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        client = TCPClientTransport(*reserved.getsockname(), connect_timeout=0.1)
        try:
            with pytest.raises((TransportConnectionError, TransportTimeoutError)):
                client.connect()
        finally:
            client.close()
