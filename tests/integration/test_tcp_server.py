"""Follower-side server tested with an independent raw socket client."""

import socket
import struct
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.transport import TCPServerTransport, TransportTimeoutError
from src.transport.framing import decode_body, encode_frame


def test_server_round_trip():
    server = TCPServerTransport(connect_timeout=1)
    try:
        address = server.listen()
        with socket.create_connection(address, timeout=1) as client:
            server.connect()
            server.connect()
            client.sendall(encode_frame({"payload": {"command": "HOLD"}}))
            assert server.receive(1) == {"payload": {"command": "HOLD"}}
            with ThreadPoolExecutor(max_workers=1) as executor:
                result = executor.submit(client.recv, 4096)
                server.send({"payload": {"response": "COMPLY"}})
                frame = result.result(1)
            size = struct.unpack("!I", frame[:4])[0]
            while len(frame) < size + 4:
                frame += client.recv(4096)
            assert decode_body(frame[4:]) == {"payload": {"response": "COMPLY"}}
    finally:
        server.close()


def test_accept_timeout():
    server = TCPServerTransport(connect_timeout=0.02)
    try:
        with pytest.raises(TransportTimeoutError):
            server.connect()
    finally:
        server.close()
