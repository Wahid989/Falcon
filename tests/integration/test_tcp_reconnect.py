"""Recover a real TCP link when a server restarts on its original port."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from src.transport import (
    ReconnectingTransport,
    TCPClientTransport,
    TCPServerTransport,
    TransportClosedError,
)


def test_server_restart_and_registration():
    server = TCPServerTransport(connect_timeout=1)
    address = server.listen()
    registrations = []
    retry_waiting = Event()
    retry = ReconnectingTransport(
        lambda: TCPClientTransport(*address, connect_timeout=0.1),
        max_retries=10,
        initial_backoff=0.01,
        max_backoff=0.02,
        on_connect=lambda link: (
            registrations.append(True),
            link.send({"register": True}),
        ),
    )
    replacement = None
    try:
        retry.connect()
        server.connect()
        assert server.receive(1) == {"register": True}
        server.close()
        with pytest.raises(TransportClosedError):
            retry.receive(1)
        original_wait = retry._closed.wait

        def observed_wait(delay):
            retry_waiting.set()
            return original_wait(delay)

        retry._closed.wait = observed_wait
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(retry.reconnect)
            assert retry_waiting.wait(1)
            replacement = TCPServerTransport(*address, connect_timeout=1)
            replacement.listen()
            result.result(timeout=2)
            replacement.connect()
        assert replacement.receive(1) == {"register": True}
        replacement.send({"restored": True})
        assert retry.receive(1) == {"restored": True}
        assert len(registrations) == 2
    finally:
        retry.close()
        server.close()
        if replacement is not None:
            replacement.close()
