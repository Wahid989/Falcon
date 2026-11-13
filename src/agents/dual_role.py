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

    def __init__(self, peer_id: str, link: Transport) -> None:
        self.peer_id = peer_id
        self._link = link
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

    def receive(self, timeout: float) -> Message:
        if self._closed.is_set() and self._inbox.empty():
            raise TransportClosedError("Peer link is closed")
        try:
            message = self._inbox.get(timeout=timeout)
        except queue.Empty:
            raise TransportTimeoutError("No message from peer in time") from None
        if message is None:
            raise TransportClosedError("Peer link is closed")
        return message

    def close(self) -> None:
        if not self._closed.is_set():
            self._closed.set()
            self._inbox.put(None)

    def deliver(self, message: Message) -> None:
        """Called by the router."""
        self._inbox.put(message)


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
        self._on_reject = on_reject
        self.rejected: deque[tuple[Message, str]] = deque(maxlen=KEEP_REJECTED)
        self.incomplete: deque[RelayIncomplete] = deque(maxlen=KEEP_REJECTED)
        self.undeliverable: deque[Message] = deque(maxlen=KEEP_REJECTED)
        self.roles = roles
        self._roles_lock = threading.Lock()
        self.relaying: RelayingDecision | None = None

        self._links: dict[str, Transport] = dict(follower_links)
        self._views: dict[str, PeerLink] = {
            peer: PeerLink(peer, link) for peer, link in follower_links.items()
        }
        if parent_id is not None:
            self._links[parent_id] = parent_link
            self._views[parent_id] = PeerLink(parent_id, parent_link)

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
        self._readers: list[threading.Thread] = []
        self._sync_epoch()

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
                    replies = self.roles.handle(message)
                    self._sync_epoch()
                    self._send_all(replies)
                    return "roles"
                self.roles.observe(message)
                self._sync_epoch()
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
        return request

    def start(self) -> None:
        """Start one reader per real link."""
        if self._readers:
            return
        for peer, link in self._links.items():
            reader = threading.Thread(
                target=self._read, args=(peer, link), daemon=True, name=f"{peer}"
            )
            reader.start()
            self._readers.append(reader)

    def stop(self, timeout: float = 2.0) -> None:
        """Stop routing and close the module views. The real links stay open."""
        self._stop.set()
        for reader in self._readers:
            reader.join(timeout)
        self._readers.clear()
        for view in self._views.values():
            view.close()

    def __enter__(self) -> DualRoleAgent:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def serve_once(self, timeout: float):
        """Handle one message from the commander above; see ``Follower.handle_once``.

        With a relay timeout set, a command this agent complies with is first
        passed to its followers. If one of them doesn't answer properly, no
        answer goes upward: the relay is kept in ``incomplete`` and None is
        returned.
        """
        if self.follower is None:
            raise DualRoleError(f"{self.agent_id} is the top; it has no commander")
        try:
            return self.follower.handle_once(timeout)
        except RelayIncomplete as incomplete:
            self.incomplete.append(incomplete)
            return None

    # --- internals -----------------------------------------------------------

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
            except TransportError:
                self.undeliverable.append(message)

    def _read(self, peer: str, link: Transport) -> None:
        while not self._stop.is_set():
            try:
                message = link.receive(timeout=POLL_SECONDS)
            except TransportTimeoutError:
                continue
            except TransportClosedError:
                self._views[peer].close()
                return
            except TransportError:
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
        if self._on_reject is not None:
            self._on_reject(message, reason)
        reply = self._error_reply(message, arrived_from, reason)
        if reply is not None:
            try:
                self._links[arrived_from].send(reply)
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
