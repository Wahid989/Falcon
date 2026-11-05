"""Every byte boundary, combined frames, and hostile frame/JSON inputs."""

import struct

import pytest

from src.transport import TransportMessageError
from src.transport.framing import FrameBuffer, encode_frame


MESSAGES = [{"text": "héllo 🦅", "nested": [True, None, {"n": 3}]}, {}]
STREAM = b"".join(encode_frame(message) for message in MESSAGES)


@pytest.mark.parametrize("boundary", range(len(STREAM) + 1))
def test_every_split_boundary(boundary):
    buffer = FrameBuffer()
    actual = []
    for chunk in (STREAM[:boundary], STREAM[boundary:]):
        buffer.feed(chunk)
        while (message := buffer.pop()) is not None:
            actual.append(message)
    assert actual == MESSAGES
    assert not buffer.data


def test_byte_at_a_time():
    buffer = FrameBuffer()
    actual = []
    for byte in STREAM:
        buffer.feed(bytes([byte]))
        while (message := buffer.pop()) is not None:
            actual.append(message)
    assert actual == MESSAGES


@pytest.mark.parametrize(
    "body", [b"[]", b"{", b"\xff", b'{"x":NaN}', b'{"x":1e999}', b'{"x":1,"x":2}']
)
def test_bad_json(body):
    buffer = FrameBuffer()
    buffer.feed(struct.pack("!I", len(body)) + body)
    with pytest.raises(TransportMessageError):
        buffer.pop()


@pytest.mark.parametrize("length", [0, 101, 0xFFFFFFFF])
def test_bad_length(length):
    buffer = FrameBuffer(max_bytes=100)
    buffer.feed(struct.pack("!I", length))
    with pytest.raises(TransportMessageError):
        buffer.pop()


def test_outbound_size_and_json_limits():
    with pytest.raises(TransportMessageError):
        encode_frame({"x": "large"}, max_bytes=2)
    with pytest.raises(TransportMessageError):
        encode_frame({"x": (1, 2)})
