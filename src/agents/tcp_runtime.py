"""Agent-level TCP registration, persistent serving, and explicit recovery.

Registration reuses REPORT_STATUS/STATUS_RESPONSE; no transport schema policy
or automatic replay is introduced. Decision policy remains injected.
"""

from __future__ import annotations

from copy import deepcopy
import logging
import socket
from threading import Event, Lock
from typing import Callable

from src.agents.commander import Commander, CommandResult, Outcome
from src.agents.follower import Follower, FollowerError
from src.decision import DecisionEngine
from src.state import FollowerState
from src.transport import (
    ReconnectingTransport,
    TCPClientTransport,
    TCPServerTransport,
    Transport,
    TransportClosedError,
    TransportConnectionError,
    TransportError,
    TransportMessageError,
    TransportTimeoutError,
)
from src.transport.tcp import _timeout

_LOGGER = logging.getLogger(__name__)
Reporter = Callable[[dict], None]


def _ignore_event(record: dict) -> None:
    pass


class CommandInterruptedError(TransportError):
    """Delivery is uncertain; recovery never replays the interrupted command."""

    def __init__(self, message_id, recovered, recovery_error=None):
        super().__init__("Command delivery is uncertain; command was not replayed")
        self.message_id = message_id
        self.recovered = recovered
        self.recovery_error = recovery_error


class CommanderSession:
    """One commander/follower relationship, used from one command thread.

    close() may be called from another thread to interrupt socket/backoff waits.
    Each registration checks peer identity, response correlation, and current epoch.
    """

    def __init__(
        self,
        agent_id: str,
        follower_id: str,
        host: str,
        port: int,
        *,
        role_epoch: int = 0,
        timeout: float = 5,
        connect_timeout: float = 5,
        max_retries: int = 5,
        initial_backoff: float = 1,
        max_backoff: float = 30,
        report: Reporter = _ignore_event,
        exchange_log=None,
    ):
        self._exchange_log = exchange_log
        self.commander = Commander(
            agent_id, role_epoch=role_epoch, exchange_log=exchange_log
        )
        # Validate identifiers/epoch before starting any connection.
        self.commander.build_command(follower_id, "REPORT_STATUS")
        self.follower_id = follower_id
        self.timeout = _timeout(timeout)
        self._report = report
        self._registered_state = None

        def make_link():
            # Resolve at the agent boundary on every attempt: a restarted
            # Compose service may receive a different IP address.
            try:
                ip = socket.gethostbyname(host)
            except OSError as exc:
                raise TransportConnectionError(f"Cannot resolve host {host!r}") from exc
            return TCPClientTransport(
                ip, port, connect_timeout=connect_timeout, io_timeout=timeout
            )

        self._transport = ReconnectingTransport(
            make_link,
            max_retries=max_retries,
            initial_backoff=initial_backoff,
            max_backoff=max_backoff,
            on_connect=self._register,
        )
        self.commander.add_follower(follower_id, self._transport)

    @property
    def registered_state(self) -> dict | None:
        return deepcopy(self._registered_state)

    @property
    def unreachable(self) -> bool:
        return self._transport.unreachable

    def _register(self, link: Transport) -> None:
        probe = Commander(
            self.commander.agent_id,
            role_epoch=self.commander.role_epoch,
            exchange_log=self._exchange_log,
        )
        probe.add_follower(self.follower_id, link)
        result = probe.issue(self.follower_id, "REPORT_STATUS", timeout=self.timeout)
        state = result.state
        response = result.response or {}
        if (
            result.outcome is not Outcome.STATUS_RESPONSE
            or not isinstance(state, dict)
            or state.get("agent_id") != self.follower_id
            or state.get("role_epoch") != self.commander.role_epoch
            or state.get("current_role") != "follower"
            or response.get("sender_role") != "follower"
            or response.get("role_epoch") != self.commander.role_epoch
        ):
            raise TransportConnectionError(
                "Follower registration failed identity/epoch checks"
            )
        self._registered_state = deepcopy(state)
        _LOGGER.info(
            "Follower %s registered at epoch %d",
            self.follower_id,
            self.commander.role_epoch,
        )
        self._report(
            {
                "event": "registered",
                "follower": self.follower_id,
                "state": deepcopy(state),
            }
        )

    def connect(self) -> None:
        try:
            self._transport.connect()
        except TransportError:
            self._registered_state = None
            raise

    def reconnect(self) -> None:
        self._registered_state = None
        # Correlation state from the previous TCP session is no longer usable.
        self.commander.remove_follower(self.follower_id)
        self.commander.add_follower(self.follower_id, self._transport)
        try:
            self._transport.reconnect()
        except TransportError:
            self._report(
                {
                    "event": "recovery_failed",
                    "follower": self.follower_id,
                    "unreachable": self.unreachable,
                }
            )
            raise

    def _interrupted(self, request, error):
        self._registered_state = None
        message_id = request["message_id"] if request is not None else None
        self._report(
            {
                "event": "connection_lost",
                "follower": self.follower_id,
                "message_id": message_id,
            }
        )
        recovery_error = None
        try:
            self.reconnect()
        except TransportError as exc:
            recovery_error = exc
        raise CommandInterruptedError(
            message_id, recovery_error is None, recovery_error
        ) from error

    def issue(self, command: str, *, waypoint: str | None = None) -> CommandResult:
        """Run one new command; a broken link triggers recovery, never a replay."""
        self.connect()
        request = None
        try:
            request = self.commander.send_command(
                self.follower_id, command, waypoint=waypoint
            )
        except (
            TransportClosedError,
            TransportMessageError,
            TransportTimeoutError,
        ) as exc:
            self._interrupted(request, exc)
        try:
            return self.commander.await_response(request, self.timeout)
        except (TransportClosedError, TransportMessageError) as exc:
            self._interrupted(request, exc)
        # An ordinary response timeout returns Outcome.TIMEOUT; it does not
        # trigger reconnect or claim that the peer is dead.

    def close(self) -> None:
        self._transport.close()
        self._registered_state = None


class FollowerTCPServer:
    """Run the real Follower over sequential TCP connections, preserving live state."""

    def __init__(
        self,
        agent_id: str,
        commander_id: str,
        decision_engine: DecisionEngine,
        *,
        state: FollowerState | None = None,
        host: str = "127.0.0.1",
        port: int = 5000,
        poll_timeout: float = 0.1,
        report: Reporter = _ignore_event,
        exchange_log=None,
    ):
        self._host, self._port = host, port
        self._poll_timeout = _timeout(poll_timeout)
        if not self._poll_timeout:
            raise ValueError("poll_timeout must be positive")
        self._closed = Event()
        self._lock = Lock()
        self._report = report
        self._link = self._new_link()
        state = deepcopy(state) if state is not None else FollowerState()
        if state.parent_id not in (None, commander_id):
            raise ValueError("State parent_id disagrees with configured commander")
        state.parent_id = commander_id
        self.follower = Follower(
            agent_id,
            self._link,
            decision_engine,
            state=state,
            exchange_log=exchange_log,
        )
        Commander(commander_id).build_command(agent_id, "REPORT_STATUS")

    def _new_link(self):
        return TCPServerTransport(
            self._host, self._port, connect_timeout=self._poll_timeout
        )

    def listen(self) -> tuple[str, int]:
        with self._lock:
            if self._closed.is_set():
                raise TransportClosedError("Follower server is closed")
            address = self._link.listen()
            self._port = address[1]
            return address

    def serve(self, *, max_commands: int | None = None) -> None:
        """Serve until close or a command limit; registration probes do not count."""
        if max_commands is not None and (
            type(max_commands) is not int or max_commands < 1
        ):
            raise ValueError("max_commands must be a positive integer")
        count = 0
        try:
            while not self._closed.is_set():
                self.listen()
                link = self._link
                try:
                    link.connect()
                except TransportTimeoutError:
                    continue
                registered = False
                self.follower.transport = link
                try:
                    while not self._closed.is_set():
                        try:
                            result = self.follower.handle_once(self._poll_timeout)
                        except TransportTimeoutError:
                            continue
                        if result is None:
                            continue
                        is_registration = (
                            not registered
                            and result.request["payload"].get("command")
                            == "REPORT_STATUS"
                            and result.response["payload"].get("response")
                            == "STATUS_RESPONSE"
                        )
                        self._report(
                            {
                                "event": "registered" if is_registration else "command",
                                "request": result.request,
                                "response": result.response,
                                "state": self.follower.status_snapshot(),
                            }
                        )
                        if is_registration:
                            registered = True
                        else:
                            count += 1
                        if max_commands is not None and count >= max_commands:
                            return
                except (
                    TransportClosedError,
                    TransportMessageError,
                    FollowerError,
                ) as exc:
                    if not self._closed.is_set():
                        _LOGGER.info("Follower connection ended: %s", exc)
                finally:
                    link.close()
                with self._lock:
                    if not self._closed.is_set():
                        self._link = self._new_link()
        except TransportClosedError:
            if not self._closed.is_set():
                raise
        finally:
            self.close()

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            self._link.close()
