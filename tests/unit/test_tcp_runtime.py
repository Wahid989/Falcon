"""Session behavior with a transport boundary that can time out deterministically."""

from unittest.mock import Mock

from src.agents import Outcome
from src.agents.tcp_runtime import CommanderSession
from src.transport import TransportTimeoutError


def test_idle_response_timeout_does_not_reconnect():
    session = CommanderSession("commander", "follower-1", "127.0.0.1", 5000)
    # Keep the real Commander's send/correlation path; replace socket operations.
    session._transport.connect = Mock()
    session._transport.send = Mock()
    session._transport.receive = Mock(side_effect=TransportTimeoutError("idle"))
    session._transport.reconnect = Mock()
    try:
        result = session.issue("HOLD")
        assert result.outcome is Outcome.TIMEOUT
        session._transport.send.assert_called_once()
        session._transport.reconnect.assert_not_called()
        assert not session.unreachable
    finally:
        session.close()


def test_invalid_command_does_not_send_or_reconnect():
    from src.agents.commander import InvalidCommandError
    import pytest

    session = CommanderSession("commander", "follower-1", "127.0.0.1", 5000)
    session._transport.connect = Mock()
    session._transport.send = Mock()
    session._transport.reconnect = Mock()
    try:
        with pytest.raises(InvalidCommandError):
            session.issue("PROCEED_TO_WAYPOINT")
        session._transport.send.assert_not_called()
        session._transport.reconnect.assert_not_called()
    finally:
        session.close()


def test_session_resolves_hostname_again_after_reconnect(monkeypatch):
    resolver = Mock(side_effect=["127.0.0.1", "127.0.0.2"])
    endpoint = Mock()
    factory = Mock(return_value=endpoint)
    monkeypatch.setattr("src.agents.tcp_runtime.socket.gethostbyname", resolver)
    monkeypatch.setattr("src.agents.tcp_runtime.TCPClientTransport", factory)
    monkeypatch.setattr(CommanderSession, "_register", lambda *_: None)
    session = CommanderSession("commander", "follower-1", "follower", 5000)
    try:
        session.connect()
        session.reconnect()
        assert resolver.call_args_list[0].args == ("follower",)
        assert resolver.call_count == 2
        assert [call.args[0] for call in factory.call_args_list] == [
            "127.0.0.1",
            "127.0.0.2",
        ]
    finally:
        session.close()
