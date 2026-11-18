"""Idle heartbeat timing and the RoleManager handoff; no election logic here."""

from copy import deepcopy
from datetime import datetime, timezone
from math import isfinite
from threading import Lock
from time import monotonic
from uuid import uuid4

STATUS_FIELDS = frozenset(
    {
        "agent_id",
        "battery_percent",
        "obstacle_detected",
        "mechanical_fault",
        "terrain",
        "capabilities",
        "position",
        "movement_flag",
        "current_command",
        "current_role",
        "role_epoch",
    }
)


class HeartbeatMonitor:
    """Serialize received()/poll() with the RoleManager's runtime lock.

    sent() can also be called from the module's command thread. Defaults are
    idle 5 seconds and 3 missed intervals. EOF alone never elects a successor.
    """

    def __init__(
        self,
        roles,
        status,
        validate,
        *,
        interval=5.0,
        missed_intervals=3,
        clock=monotonic,
        on_lost=lambda: None
    ):
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not isfinite(interval)
            or interval <= 0
        ):
            raise ValueError("Heartbeat interval must be positive and finite")
        if type(missed_intervals) is not int or missed_intervals < 1:
            raise ValueError("Missed intervals must be a positive integer")
        self.roles, self.status, self.validate = roles, status, validate
        self.interval, self.missed_intervals = interval, missed_intervals
        self.clock, self.on_lost = clock, on_lost
        self._parent = roles.get_current_commander()
        self._last_parent = clock()
        self._lost = False
        self._disconnected = False
        self._activity_lock = Lock()
        self._activity = {peer: clock() for peer in roles.known_peers()}

    def _sync_parent(self):
        parent = self.roles.get_current_commander()
        if parent != self._parent:
            self._parent = parent
            self._last_parent = self.clock()
            self._lost = False
            self._disconnected = False
        return parent

    @property
    def parent_unavailable(self):
        return self._disconnected or self._lost

    def disconnected(self, peer):
        """Hold on confirmed EOF immediately, but keep the original loss deadline.

        A reconnect must deliver valid parent traffic to restore liveness. EOF
        alone does not shorten the missed-interval succession window.
        """
        if peer == self._sync_parent() and not self.parent_unavailable:
            self._disconnected = True
            self.on_lost()

    def sent(self, peer):
        with self._activity_lock:
            self._activity[peer] = self.clock()

    def received(self, message, peer):
        """Only known-peer, schema-valid, current-epoch traffic counts."""
        parent = self._sync_parent()
        relation = self.roles.known_peers().get(peer)
        if (
            relation is None
            or not self.validate(message)[0]
            or message.get("sender_id") != peer
            or message.get("recipient_id") != self.roles.agent_id
            or message.get("sender_role") != relation
            or message.get("role_epoch") != self.roles.role_epoch
        ):
            return False
        payload = message["payload"]
        if message["type"] == "heartbeat":
            if peer == parent:
                if "status_summary" not in payload:
                    return False
                for snapshot in payload["status_summary"]:
                    agent = snapshot.get("agent_id")
                    if agent in self.roles.hierarchy and agent != parent:
                        self.roles.observe_status(agent, deepcopy(snapshot))
            else:
                snapshot = payload.get("status")
                if snapshot is None or snapshot.get("agent_id", peer) != peer:
                    return False
                self.roles.observe_status(peer, deepcopy(snapshot))
        if message["type"] != "heartbeat":
            # Opposing heartbeat traffic must not starve our own outgoing beats.
            self.sent(peer)
        if peer == parent:
            self._last_parent = self.clock()
            if not self._lost:
                self._disconnected = False
        return True

    def _message(self, peer, payload, role):
        message = {
            "message_id": str(uuid4()),
            "sender_id": self.roles.agent_id,
            "recipient_id": peer,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "schema_version": 1,
            "sender_role": role,
            "role_epoch": self.roles.role_epoch,
            "type": "heartbeat",
            "payload": payload,
        }
        ok, problems = self.validate(message)
        if not ok:
            raise ValueError("Invalid heartbeat status: " + "; ".join(problems))
        return message

    def poll(self):
        """Return messages to send, including announcements returned by failover."""
        parent = self._sync_parent()
        snapshot = {
            key: deepcopy(value)
            for key, value in self.status().items()
            if key in STATUS_FIELDS
        }
        snapshot["agent_id"] = self.roles.agent_id
        self.roles.observe_status(self.roles.agent_id, snapshot)
        now = self.clock()
        messages = []
        if (
            parent is not None
            and not self._lost
            and now - self._last_parent >= self.interval * self.missed_intervals
        ):
            self._lost = True  # Once per loss, even if no successor can promote.
            if not self._disconnected:
                self.on_lost()
            held = {
                key: deepcopy(value)
                for key, value in self.status().items()
                if key in STATUS_FIELDS
            }
            held["agent_id"] = self.roles.agent_id
            self.roles.observe_status(self.roles.agent_id, held)
            messages.extend(self.roles.on_commander_lost(parent))
            parent = self._sync_parent()
        peers = list(self.roles.followers())
        if parent is not None and not self._lost:
            peers.append(parent)
        summary = [
            dict(deepcopy(self.roles.peer_status[agent]), agent_id=agent)
            for agent in self.roles.followers()
            if agent in self.roles.peer_status
        ]
        for peer in peers:
            with self._activity_lock:
                previous = self._activity.setdefault(peer, now)
            if now - previous < self.interval:
                continue
            payload = (
                {"status": snapshot} if peer == parent else {"status_summary": summary}
            )
            messages.append(
                self._message(
                    peer, payload, "follower" if peer == parent else "commander"
                )
            )
            # Avoid spinning on unavailable links; wait one interval to try again.
            self.sent(peer)
        return messages
