"""Backoff schedule, registration hooks, exhaustion, and cancellation."""

from concurrent.futures import ThreadPoolExecutor
import logging
from threading import Event
from unittest.mock import Mock

import pytest

from src.transport import (
    ReconnectingTransport,
    Transport,
    TransportClosedError,
    TransportConnectionError,
    TransportUnreachableError,
)


def test_backoff_cap_and_exhaustion(caplog):
    links = []

    def factory():
        link = Mock(spec=Transport)
        link.connect.side_effect = TransportConnectionError("unavailable")
        links.append(link)
        return link

    retry = ReconnectingTransport(factory, max_retries=8)
    delays = []
    retry._closed.wait = lambda delay: delays.append(delay) or False
    with caplog.at_level(logging.WARNING):
        with pytest.raises(TransportUnreachableError):
            retry.connect()
    assert delays == [1, 2, 4, 8, 16, 30, 30, 30]
    assert len(links) == 9
    assert retry.unreachable
    assert "retrying in 30.000 seconds" in caplog.text
    assert "Peer unreachable after 9 attempts" in caplog.text
    for link in links:
        link.close.assert_called_once()
    retry.close()


def test_registration_on_each_connection_and_no_replay():
    factory = Mock(side_effect=[Mock(spec=Transport), Mock(spec=Transport)])
    register = Mock()
    retry = ReconnectingTransport(factory, max_retries=0, on_connect=register)
    retry.connect()
    retry.connect()
    first = register.call_args.args[0]
    first.send.side_effect = TransportClosedError("lost")
    with pytest.raises(TransportClosedError):
        retry.send({"command": "HOLD"})
    assert factory.call_count == 1
    retry.reconnect()
    assert register.call_count == 2
    second = register.call_args.args[0]
    second.send.assert_not_called()
    retry.close()


def test_shutdown_interrupts_backoff():
    attempted = Event()
    link = Mock(spec=Transport)

    def fail():
        attempted.set()
        raise TransportConnectionError("unavailable")

    link.connect.side_effect = fail
    retry = ReconnectingTransport(lambda: link, initial_backoff=30)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(retry.connect)
        assert attempted.wait(1)
        retry.close()
        with pytest.raises(TransportClosedError):
            result.result(timeout=1)
    with pytest.raises(TransportClosedError):
        retry.reconnect()


def test_explicit_recovery_clears_unreachable():
    bad, good = Mock(spec=Transport), Mock(spec=Transport)
    bad.connect.side_effect = TransportConnectionError("unavailable")
    retry = ReconnectingTransport(Mock(side_effect=[bad, good]), max_retries=0)
    with pytest.raises(TransportUnreachableError):
        retry.connect()
    with pytest.raises(TransportUnreachableError):
        retry.send({})
    retry.reconnect()
    assert not retry.unreachable
    retry.send({})
    good.send.assert_called_once_with({})
    retry.close()


def test_retryable_factory_failure_uses_same_retry_budget():
    good = Mock(spec=Transport)
    factory = Mock(side_effect=[TransportConnectionError("DNS unavailable"), good])
    retry = ReconnectingTransport(factory, max_retries=1, initial_backoff=0)
    try:
        retry.connect()
        assert factory.call_count == 2
        good.connect.assert_called_once()
        assert not retry.unreachable
    finally:
        retry.close()


def test_factory_failure_marks_unreachable_without_allocated_link():
    retry = ReconnectingTransport(
        Mock(side_effect=TransportConnectionError("DNS unavailable")), max_retries=0
    )
    try:
        with pytest.raises(TransportUnreachableError):
            retry.connect()
        assert retry.unreachable
    finally:
        retry.close()
