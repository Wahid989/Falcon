"""A dual-role agent: a follower to its parent and a commander to its own followers.

Every agent carries both modules. A middle agent uses both at once: its
Follower module answers its parent, while its Commander module gives orders
to its followers. Incoming messages are routed by **who sent them**, looked up
in the hierarchy (the known-peers table), never by a wire field:

- from the agent's parent: to the Follower module;
- from one of its followers: to the Commander module;
- from anyone else: rejected with ``ERROR`` / ``INVALID_SENDER_OR_RECIPIENT``.

A message must also arrive on that peer's own link. A message on the parent's
link that claims to come from a follower is rejected the same way.

The runtime owns the real links (one per peer) and hands them in. Each module
reads from a ``PeerLink``, a view of one real link that only receives what the
router gives it.
"""

from __future__ import annotations

import queue
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping

from src.agents.commander import Commander, Validator, default_validator
from src.agents.follower import Follower
from src.agents.hierarchy import Hierarchy
from src.agents.heartbeat import HeartbeatMonitor
from src.agents.relay import Relay, RelayIncomplete, RelayingDecision
from src.decision import DecisionEngine, decide
from src.logging.exchange import ExchangeLogger
from src.state import FollowerState

if TYPE_CHECKING:
    from src.roles.manager import RoleManager

from src.transport import (
    Message,
    Transport,
    TransportClosedError,
    TransportError,
    TransportTimeoutError,
)

SCHEMA_VERSION = 1
# How many rejected messages are kept for inspection.
KEEP_REJECTED = 256
# How often each link reader wakes up to notice a stop request.
POLL_SECONDS = 0.05
ROLE_TYPES = frozenset({"role_request", "role_grant", "role_deny"})


class DualRoleError(Exception):
    """The agent's links don't match its place in the hierarchy."""


class PeerLink(Transport):
    """One module's view of the link to one peer.

    ``send`` goes straight out on the real link. ``receive`` returns only the
    messages the router has delivered for this peer.
    """

    def __init__(self, peer_id: str, link: Transport, on_send=None) -> None:
        self._on_send = on_send
        self.peer_id = peer_id
        self._link = link
        self.last_received: Message | None = None
        self._inbox: queue.Queue[Message | None] = queue.Queue()
        self._closed = threading.Event()

    def connect(self) -> None:
        # The real link is connected and owned by the runtime.
        if self._closed.is_set():
            raise TransportClosedError("Peer link is closed")

    def send(self, message: Message) -> None:
        if self._closed.is_set():
            raise TransportClosedError("Peer link is closed")
        self._link.send(message)
        if self._on_send is not None:
            self._on_send(self.peer_id)

    def receive(self, timeout: float) -> Message:
        if self._closed.is_set() and self._inbox.empty():
            raise TransportClosedError("Peer link is closed")
        try:
            message = self._inbox.get(timeout=timeout)
        except queue.Empty:
            raise TransportTimeoutError("No message from peer in time") from None
        if message is None:
            raise TransportClosedError("Peer link is closed")
        self.last_received = message
        return message

    def close(self) -> None:
        if not self._closed.is_set():
            self._closed.set()
            self._inbox.put(None)

    def deliver(self, message: Message) -> None:
        """Called by the router."""
        self._inbox.put(message)

    def reopen(self, link: Transport) -> None:
        """Carry on over a fresh real link (after the old one died)."""
        self._link = link
        self._inbox = queue.Queue()
        self._closed.clear()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()


def exchange_logger_for(
    hierarchy: Hierarchy, agent_id: str, directory: str | Path, *, session_id: str
) -> ExchangeLogger:
    """An exchange log that stamps hierarchy levels from the hierarchy."""
    return ExchangeLogger(
        directory,
        agent_id,
        session_id=session_id,
        level=hierarchy.level_of(agent_id),
        peer_levels=hierarchy.peer_levels(agent_id),
        hierarchy=hierarchy,
    )


class DualRoleAgent:
    """Runs an agent's Follower and Commander modules over its real links.

    ``parent_link`` is required unless the agent is the top. ``follower_links``
    must have exactly one link per follower the hierarchy gives this agent.
    Call ``start()`` to begin routing and ``stop()`` to end it; the real links
    stay open (the runtime closes them).

    ``follower`` is ``None`` for the top, and ``commander`` has no followers
    for a leaf. Use ``commander`` from one thread and drive ``follower`` (its
    ``handle_once``) from one thread, as each module expects.

    With ``relay_timeout`` set, a middle agent passes each command it accepts
    down to its followers and answers upward with a summary (see
    ``src/agents/relay.py``). The relay uses ``commander`` from the follower
    thread, so don't also drive ``commander`` directly while serving.
    """

    def __init__(
        self,
        agent_id: str,
        hierarchy: Hierarchy,
        *,
        parent_link: Transport | None = None,
        follower_links: Mapping[str, Transport] | None = None,
        decision_engine: DecisionEngine | None = None,
        state: FollowerState | None = None,
        validator: Validator | None = None,
        exchange_log: ExchangeLogger | None = None,
        on_reject: Callable[[Message, str], None] | None = None,
        relay_timeout: float | None = None,
        on_relay: Callable[[Relay], None] | None = None,
        roles: RoleManager | None = None,
        heartbeat_interval: float | None = None,
        heartbeat_misses: int = 3,
        heartbeat_status: Callable[[], Mapping[str, object]] | None = None,
    ) -> None:
        follower_links = dict(follower_links or {})
        parent_id = hierarchy.parent_of(agent_id)
        followers = hierarchy.children_of(agent_id)
        if (parent_link is None) != (parent_id is None):
            raise DualRoleError(
                f"{agent_id}: a parent link is needed exactly when the agent has a"
                f" commander (its commander is {parent_id!r})"
            )
        if set(follower_links) != set(followers):
            raise DualRoleError(
                f"{agent_id}: follower links {sorted(follower_links)} don't match"
                f" its followers {list(followers)}"
            )

        self.agent_id = agent_id
        self.hierarchy = hierarchy
        self._validate = validator if validator is not None else default_validator()
        self._exchange_log = exchange_log
        self._on_reject = on_reject
        self.rejected: deque[tuple[Message, str]] = deque(maxlen=KEEP_REJECTED)
        self.incomplete: deque[RelayIncomplete] = deque(maxlen=KEEP_REJECTED)
        self.undeliverable: deque[Message] = deque(maxlen=KEEP_REJECTED)
        self._heartbeat = None
        self._heartbeat_thread = None
        self.roles = roles
        if exchange_log is not None:
            exchange_log.bind_hierarchy(
                lambda: roles.hierarchy if roles is not None else self.hierarchy
            )
            if roles is not None:
                roles.add_record_sink(exchange_log.write_record)
        self._roles_lock = threading.Lock()
        self.relaying: RelayingDecision | None = None

        self._links: dict[str, Transport] = dict(follower_links)
        self._views: dict[str, PeerLink] = {
            peer: PeerLink(peer, link, self._note_sent)
            for peer, link in follower_links.items()
        }
        if parent_id is not None:
            self._links[parent_id] = parent_link
            self._views[parent_id] = PeerLink(parent_id, parent_link, self._note_sent)

        self.commander = Commander(
            agent_id,
            role_epoch=hierarchy.role_epoch,
            validator=self._validate,
            exchange_log=exchange_log,
        )
        for follower_id in followers:
            self.commander.add_follower(follower_id, self._views[follower_id])

        self.follower: Follower | None = None
        if parent_id is not None:
            state = state if state is not None else FollowerState()
            state.parent_id = parent_id
            state.follower_ids = list(followers)
            state.hierarchy_level = hierarchy.level_of(agent_id)
            state.role_epoch = max(state.role_epoch, hierarchy.role_epoch)
            engine = decision_engine if decision_engine is not None else decide
            if relay_timeout is not None and followers:
                self.relaying = RelayingDecision(
                    self.commander,
                    timeout=relay_timeout,
                    own_decision=engine,
                    on_relay=on_relay,
                )
                engine = self.relaying
            self.follower = Follower(
                agent_id,
                self._views[parent_id],
                decision_engine=engine,
                state=state,
                validator=self._validate,
                exchange_log=exchange_log,
            )

        self._stop = threading.Event()
        self._readers: dict[str, threading.Thread] = {}
        self._started = False
        self._sync_epoch()
        if heartbeat_interval is not None:
            if roles is None:
                raise ValueError("Heartbeat monitoring requires a RoleManager")
            if heartbeat_status is None:
                if self.follower is None:
                    raise ValueError(
                        "Top agent heartbeat requires a live status provider"
                    )
                heartbeat_status = self.follower.status_snapshot
            self._heartbeat = HeartbeatMonitor(
                roles,
                heartbeat_status,
                self._validate,
                interval=heartbeat_interval,
                missed_intervals=heartbeat_misses,
                on_lost=self._hold_on_loss,
            )

    # --- routing -------------------------------------------------------------

    def route(self, message: Message, arrived_from: str) -> str | None:
        """Which module handles ``message``: ``"follower"``, ``"commander"`` or None.

        ``arrived_from`` is the peer whose link carried the message.
        """
        sender = message.get("sender_id") if isinstance(message, dict) else None
        if not isinstance(sender, str) or sender != arrived_from:
            return None
        relation = self.hierarchy.relation(self.agent_id, sender)
        if relation == "parent":
            return "follower"
        if relation == "follower":
            return "commander"
        return None

    def dispatch(self, message: Message, arrived_from: str) -> str | None:
        """Route one received message to its module, or reject it.

        With a Role Manager, role messages go to it (``"roles"``), and every
        other message's epoch is shown to it first, so a newer epoch reaches
        both modules before they handle the message.
        """
        target = self.route(message, arrived_from)
        if target is None:
            self._reject(message, arrived_from)
            return None
        if self.roles is not None:
            with self._roles_lock:
                if message.get("type") in ROLE_TYPES:
                    if self._heartbeat is not None:
                        self._heartbeat.received(message, arrived_from)
                    if self._exchange_log is not None:
                        self._exchange_log.record("received", message)
                    ignored = self.roles.ignored[-1] if self.roles.ignored else None
                    replies = self.roles.handle(message)
                    if (
                        self._exchange_log is not None
                        and self.roles.ignored
                        and self.roles.ignored[-1] is not ignored
                    ):
                        self._exchange_log.event(
                            "ROLE_MESSAGE_REJECTED",
                            {
                                "message_id": message.get("message_id"),
                                "sender_id": arrived_from,
                                "reason": self.roles.ignored[-1][1],
                            },
                        )
                    self._sync_epoch()
                    self._send_all(replies)
                    return "roles"
                if (
                    message.get("type") == "heartbeat"
                    and not self._validate(message)[0]
                ):
                    return None
                accepted = self.roles.observe(message)
                self._sync_epoch()
                if self._heartbeat is not None:
                    observed = accepted and self._heartbeat.received(
                        message, arrived_from
                    )
                    if message.get("type") == "heartbeat":
                        return "heartbeat" if observed else None
        self._views[arrived_from].deliver(message)
        return target

    def request_role(self, target_id: str | None = None) -> Message:
        """Ask a commander above for the role, over the link to it.

        The target must be a direct peer (this agent's commander): a
        skip-level request needs a link to the top, which the runtime would
        have to open.
        """
        if self.roles is None:
            raise DualRoleError(f"{self.agent_id} has no Role Manager")
        with self._roles_lock:
            request = self.roles.request_role(target_id)
            if request["recipient_id"] not in self._links:
                self.roles.cancel_request()
                raise DualRoleError(
                    f"{self.agent_id} has no link to {request['recipient_id']!r}"
                )
            self._links[request["recipient_id"]].send(request)
            self._note_sent(request["recipient_id"])
            if self._exchange_log is not None:
                self._exchange_log.record("sent", request)
        return request

    def start(self) -> None:
        """Start one reader per real link."""
        if self._started:
            return
        self._started = True
        for peer, link in self._links.items():
            self._start_reader(peer, link)
        if self._heartbeat is not None:
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, daemon=True, name="heartbeat"
            )
            self._heartbeat_thread.start()

    def replace_link(self, peer_id: str, link: Transport) -> None:
        """Swap in a fresh link to one peer, for example after it crashed.

        Only that peer's side is touched: the module using it carries on over
        the new link, and every other link keeps running.
        """
        if peer_id not in self._links:
            raise DualRoleError(f"{self.agent_id} has no link to {peer_id!r}")
        self._links[peer_id] = link
        self._views[peer_id].reopen(link)
        old = self._readers.pop(peer_id, None)
        if old is not None:
            old.join(POLL_SECONDS * 4)
        if self._started and not self._stop.is_set():
            self._start_reader(peer_id, link)

    def link_is_down(self, peer_id: str) -> bool:
        """True once the link to ``peer_id`` has closed."""
        return self._views[peer_id].closed

    def stop(self, timeout: float = 2.0) -> None:
        """Stop routing and close the module views. The real links stay open."""
        self._stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout)
        for reader in self._readers.values():
            reader.join(timeout)
        self._readers.clear()
        for view in self._views.values():
            view.close()
        if self._exchange_log is not None:
            if self.roles is not None:
                with self._roles_lock:
                    self.roles.remove_record_sink(self._exchange_log.write_record)
            self._exchange_log.flush()

    def __enter__(self) -> DualRoleAgent:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def serve_once(self, timeout: float):
        """Handle one message from the commander above; see ``Follower.handle_once``.

        With a relay timeout set, a command this agent complies with is first
        passed to its followers. If one of them doesn't answer properly, the
        answer upward is ``ERROR`` / ``NO_RESPONSE`` naming that follower in
        ``error.agent``; the relay is kept in ``incomplete`` and None is
        returned.
        """
        if self.follower is None:
            raise DualRoleError(f"{self.agent_id} is the top; it has no commander")
        try:
            return self.follower.handle_once(timeout)
        except RelayIncomplete as incomplete:
            self.incomplete.append(incomplete)
            self._report_no_response(incomplete)
            return None

    # --- internals -----------------------------------------------------------

    def _note_sent(self, peer: str) -> None:
        if self._heartbeat is not None:
            self._heartbeat.sent(peer)

    def _hold_on_loss(self) -> None:
        if self.follower is not None:
            self.follower.state.movement_flag = False
            self.follower.state.current_command = None

    def poll_heartbeats(self) -> None:
        """Run the heartbeat/failover callbacks under the RoleManager lock."""
        if self._heartbeat is not None:
            with self._roles_lock:
                messages = self._heartbeat.poll()
                self._sync_epoch()
                self._send_all(messages)

    def _heartbeat_loop(self) -> None:
        period = min(POLL_SECONDS, self._heartbeat.interval / 4)
        while not self._stop.wait(period):
            self.poll_heartbeats()

    def _report_no_response(self, incomplete: RelayIncomplete) -> None:
        parent = self.hierarchy.parent_of(self.agent_id)
        request = self._views[parent].last_received
        if request is None or not isinstance(request.get("message_id"), str):
            return
        error = {"code": "NO_RESPONSE", "message": str(incomplete)}
        if incomplete.silent_agent is not None:
            error["agent"] = incomplete.silent_agent
        reply = {
            "message_id": str(uuid.uuid4()),
            "sender_id": self.agent_id,
            "recipient_id": parent,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "schema_version": SCHEMA_VERSION,
            "sender_role": "follower",
            "role_epoch": self.follower.state.role_epoch,
            "type": "response",
            "payload": {
                "response": "ERROR",
                "in_reply_to": request["message_id"],
                "error": error,
            },
        }
        ok, problems = self._validate(reply)
        if not ok:
            raise DualRoleError("built an invalid NO_RESPONSE: " + "; ".join(problems))
        self._views[parent].send(reply)
        if self._exchange_log is not None:
            self._exchange_log.record("sent", reply)

    def _sync_epoch(self) -> None:
        """Stamp the Role Manager's epoch on both modules."""
        if self.roles is None:
            return
        epoch = self.roles.role_epoch
        self.commander.role_epoch = epoch
        if self.follower is not None and self.follower.state.role_epoch < epoch:
            self.follower.state.role_epoch = epoch

    def _send_all(self, messages: list[Message]) -> None:
        for message in messages:
            link = self._links.get(message["recipient_id"])
            if link is None:
                self.undeliverable.append(message)
                continue
            try:
                link.send(message)
                if self._exchange_log is not None:
                    self._exchange_log.record("sent", message)
                self._note_sent(message["recipient_id"])
            except TransportError:
                self.undeliverable.append(message)

    def _start_reader(self, peer: str, link: Transport) -> None:
        reader = threading.Thread(
            target=self._read, args=(peer, link), daemon=True, name=peer
        )
        self._readers[peer] = reader
        reader.start()

    def _read(self, peer: str, link: Transport) -> None:
        # Stops when the agent stops, or when this link has been replaced.
        while not self._stop.is_set() and self._links.get(peer) is link:
            try:
                message = link.receive(timeout=POLL_SECONDS)
            except TransportTimeoutError:
                continue
            except TransportClosedError:
                if self._links.get(peer) is link:
                    self._views[peer].close()
                return
            except TransportError as exc:
                if self._exchange_log is not None:
                    self._exchange_log.event(
                        "TRANSPORT_ERROR", {"peer_id": peer, "error": str(exc)}
                    )
                # A garbled frame: the transport has already dropped it.
                continue
            self.dispatch(message, peer)

    def _reject(self, message: Message, arrived_from: str) -> None:
        sender = message.get("sender_id") if isinstance(message, dict) else None
        if self.hierarchy.relation(self.agent_id, sender) is None:
            reason = f"{sender!r} is not this agent's commander or follower"
        else:
            reason = (
                f"message claims to be from {sender!r} but came from {arrived_from!r}"
            )
        self.rejected.append((message, reason))
        if self._exchange_log is not None:
            if isinstance(message, dict):
                self._exchange_log.record("received", message)
            self._exchange_log.event(
                "UNAUTHORIZED_PEER",
                {
                    "sender_id": sender,
                    "arrived_from": arrived_from,
                    "reason": reason,
                    "message_id": (
                        message.get("message_id") if isinstance(message, dict) else None
                    ),
                },
            )
        if self._on_reject is not None:
            self._on_reject(message, reason)
        reply = self._error_reply(message, arrived_from, reason)
        if reply is not None:
            try:
                self._links[arrived_from].send(reply)
                if self._exchange_log is not None:
                    self._exchange_log.record("sent", reply)
            except TransportError:
                pass

    def _error_reply(
        self, message: Message, recipient: str, reason: str
    ) -> Message | None:
        """An ERROR response, when the message can be correlated and answered."""
        if not isinstance(message, dict) or message.get("type") == "response":
            return None  # never answer a response; that could loop
        message_id = message.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            return None
        relation = self.hierarchy.relation(self.agent_id, recipient)
        reply = {
            "message_id": str(uuid.uuid4()),
            "sender_id": self.agent_id,
            "recipient_id": recipient,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "schema_version": SCHEMA_VERSION,
            "sender_role": "commander" if relation == "follower" else "follower",
            "role_epoch": self.hierarchy.role_epoch,
            "type": "response",
            "payload": {
                "response": "ERROR",
                "in_reply_to": message_id,
                "error": {"code": "INVALID_SENDER_OR_RECIPIENT", "message": reason},
            },
        }
        ok, _ = self._validate(reply)
        return reply if ok else None
