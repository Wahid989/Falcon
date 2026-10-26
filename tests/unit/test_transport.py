"""Behavioral contract for the paired synchronous transport."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic

import pytest

from src.transport import (
    InMemoryTransport,
    Transport,
    TransportClosedError,
    TransportMessageError,
    TransportNotConnectedError,
    TransportTimeoutError,
)


@pytest.fixture
def pair():
    left, right = InMemoryTransport.pair()
    left.connect()
    right.connect()
    yield left, right
    left.close()
    right.close()


def test_interface_is_abstract():
    with pytest.raises(TypeError):
        Transport()


def test_round_trip_and_fifo(pair):
    left, right = pair
    for index in range(5):
        left.send({"sequence": index})
        right.send({"reply": index})
    assert [right.receive(0) for _ in range(5)] == [
        {"sequence": index} for index in range(5)
    ]
    assert [left.receive(0) for _ in range(5)] == [
        {"reply": index} for index in range(5)
    ]


def test_nested_payloads_are_independent(pair):
    left, right = pair
    nested = {"values": [1, {"enabled": True, "optional": None}]}
    message = {"payload": nested, "other": nested}
    left.send(message)
    left.send(message)
    nested["values"].append("sender change")
    first = right.receive(0)
    first["payload"]["values"][1]["enabled"] = False
    assert first["other"]["values"][1]["enabled"] is True
    assert right.receive(0) == {
        "payload": {"values": [1, {"enabled": True, "optional": None}]},
        "other": {"values": [1, {"enabled": True, "optional": None}]},
    }


def test_timeout_and_reuse(pair):
    left, right = pair
    started = monotonic()
    with pytest.raises(TransportTimeoutError):
        right.receive(0.03)
    assert 0.02 <= monotonic() - started < 1
    with pytest.raises(TransportTimeoutError):
        right.receive(0)
    left.send({})
    assert right.receive(0) == {}


@pytest.mark.parametrize(
    "timeout", [-1, float("inf"), float("nan"), None, True, "1", 10**1000]
)
def test_invalid_timeout(pair, timeout):
    with pytest.raises(ValueError):
        pair[0].receive(timeout)


def test_connect_and_close_lifecycle():
    left, right = InMemoryTransport.pair()
    with pytest.raises(TransportNotConnectedError):
        left.send({})
    with pytest.raises(TransportNotConnectedError):
        left.receive(0)
    left.connect()
    left.connect()
    with pytest.raises(TransportNotConnectedError):
        left.send({})
    right.connect()
    left.send({"queued": True})
    left.close()
    left.close()
    assert right.receive(0) == {"queued": True}
    for endpoint in (left, right):
        with pytest.raises(TransportClosedError):
            endpoint.connect()
        with pytest.raises(TransportClosedError):
            endpoint.send({})
        with pytest.raises(TransportClosedError):
            endpoint.receive(0)
    right.close()


def test_close_before_connect_and_unpaired_endpoint():
    left, right = InMemoryTransport.pair()
    left.close()
    with pytest.raises(TransportClosedError):
        right.connect()
    with pytest.raises(TransportNotConnectedError):
        InMemoryTransport().connect()


def test_local_close_discards_inbox(pair):
    left, right = pair
    left.send({})
    right.close()
    with pytest.raises(TransportClosedError):
        right.receive(0)


@pytest.mark.parametrize(
    "message", [[], {1: "key"}, {"x": (1, 2)}, {"x": float("nan")}, {"x": object()}]
)
def test_non_json_messages_are_rejected_without_delivery(pair, message):
    left, right = pair
    with pytest.raises(TransportMessageError):
        left.send(message)
    with pytest.raises(TransportTimeoutError):
        right.receive(0)


def test_cycles_are_rejected(pair):
    message = {}
    message["self"] = message
    with pytest.raises(TransportMessageError):
        pair[0].send(message)


@pytest.mark.parametrize("action", ["send", "local_close", "peer_close"])
def test_blocked_receiver_wakes(pair, action):
    left, right = pair
    waiting = Event()
    # Signal from inside Condition.wait so the action happens after the receiver
    # has entered its wait loop. The shared lock prevents a lost notification.
    original_wait = right._condition.wait

    def observed_wait(timeout):
        waiting.set()
        return original_wait(timeout)

    right._condition.wait = observed_wait
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(right.receive, 2)
        assert waiting.wait(1)
        if action == "send":
            left.send({"awake": True})
            assert future.result(timeout=1) == {"awake": True}
        else:
            (right if action == "local_close" else left).close()
            with pytest.raises(TransportClosedError):
                future.result(timeout=1)
