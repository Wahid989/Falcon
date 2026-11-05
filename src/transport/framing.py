"""Length-prefixed UTF-8 JSON framing, independent of sockets and schema."""

import json
import struct

from .base import Message, TransportMessageError
from ._json import _copy_json

DEFAULT_MAX_FRAME_BYTES = 1024 * 1024


def encode_frame(message: Message, max_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> bytes:
    """Snapshot a JSON object and prefix its UTF-8 byte count (unsigned big endian)."""
    if type(message) is not dict:
        raise TransportMessageError("Message must be a JSON object")
    try:
        snapshot = _copy_json(message, set())
        body = json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise TransportMessageError("Message cannot be encoded as UTF-8 JSON") from exc
    if not 0 < len(body) <= max_bytes:
        raise TransportMessageError("Message exceeds the frame size limit")
    return struct.pack("!I", len(body)) + body


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def decode_body(body: bytes) -> Message:
    """Reject malformed JSON, duplicate keys, and Python-only JSON extensions."""
    try:
        message = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
        if type(message) is not dict:
            raise ValueError("Expected a JSON object")
        return _copy_json(message, set())
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise TransportMessageError("Invalid UTF-8 JSON object") from exc


class FrameBuffer:
    """Retain incomplete bytes across timeouts; extract one frame at a time."""

    def __init__(self, max_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> None:
        if type(max_bytes) is not int or not 0 < max_bytes <= 0xFFFFFFFF:
            raise ValueError("Frame limit must be between 1 and 4294967295 bytes")
        self.max_bytes = max_bytes
        self.data = bytearray()

    def feed(self, data: bytes) -> None:
        self.data.extend(data)

    def pop(self) -> Message | None:
        if len(self.data) < 4:
            return None
        size = struct.unpack("!I", self.data[:4])[0]
        if not 0 < size <= self.max_bytes:
            raise TransportMessageError("Invalid frame length")
        if len(self.data) < 4 + size:
            return None
        body = bytes(self.data[4 : 4 + size])
        del self.data[: 4 + size]
        return decode_body(body)
