"""Registration rejects incorrect peer metadata and supports runtime shutdown."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest

from scripts.tcp_agent_policy import decide
from src.agents.tcp_runtime import CommanderSession, FollowerTCPServer
from src.state import FollowerState
from src.transport import (
    TransportClosedError,
    TransportConnectionError,
    TransportUnreachableError,
)


@pytest.mark.parametrize(
    "agent_id,parent_id,epoch,role",
    [
        ("other-follower", "commander", 0, "follower"),
        ("follower-1", "other-commander", 0, "follower"),
        ("follower-1", "commander", 1, "follower"),
        ("follower-1", "commander", 0, "commander"),
    ],
)
def test_registration_rejects_wrong_peer_or_authority(agent_id, parent_id, epoch, role):
    server = FollowerTCPServer(
        agent_id,
        parent_id,
        decide,
        port=0,
        state=FollowerState(role_epoch=epoch, current_role=role),
    )
    session = CommanderSession(
        "commander",
        "follower-1",
        *server.listen(),
        max_retries=0,
        timeout=0.5,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(server.serve)
        try:
            with pytest.raises(TransportUnreachableError):
                session.connect()
            assert session.unreachable
            assert session.registered_state is None
        finally:
            session.close()
            server.close()
            result.result(timeout=2)


def test_registered_state_is_an_independent_snapshot():
    server = FollowerTCPServer("follower-1", "commander", decide, port=0)
    session = CommanderSession("commander", "follower-1", *server.listen())
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(server.serve)
        try:
            session.connect()
            state = session.registered_state
            state["capabilities"].append("changed")
            assert session.registered_state["capabilities"] == []
        finally:
            session.close()
            server.close()
            result.result(timeout=2)


def test_runtime_shutdown_interrupts_reconnection_backoff(monkeypatch):
    attempted = Event()
    link = Mock()

    def fail():
        attempted.set()
        raise TransportConnectionError("unavailable")

    link.connect.side_effect = fail
    monkeypatch.setattr(
        "src.agents.tcp_runtime.TCPClientTransport", lambda *a, **k: link
    )
    session = CommanderSession(
        "commander",
        "follower-1",
        "127.0.0.1",
        5000,
        initial_backoff=30,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(session.connect)
        assert attempted.wait(1)
        session.close()
        with pytest.raises(TransportClosedError):
            result.result(timeout=1)
