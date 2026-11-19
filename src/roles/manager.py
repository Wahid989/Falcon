"""Role Manager: who is in charge, at which epoch, and how that changes.

Each agent has one Role Manager. It keeps the agent's view of the chain (a
``Hierarchy``), the current ``role_epoch``, and the agent's role, and it
handles the three role messages (``role_request``, ``role_grant``,
``role_deny``). It holds no transport: ``request_role`` and ``handle`` return
the messages to send, and the runtime sends them.

The agent's ``role`` follows the role state machine (docs/role-state-machine.md):

- ``commander``: the agent is the top of the chain;
- ``follower``: the agent has a commander above it;
- ``transitioning``: the agent has asked for the role and is waiting.

A middle agent is ``follower`` here and still commands its own followers
(``followers()``); the state machine tracks the upward relationship.

Rules (FALCON-045 to 051):

- A request may skip levels and go straight to the top. A grant makes the
  requester the new top; the previous top, with everything still under it,
  nests one level deeper. Only the current top can grant, so a request to an
  in-between commander is denied ``not_authorized``.
- ``role_epoch`` increases only when a request is granted (the failover
  self-promotion, a later ticket, is the other case). Never on a deny.
- The grant is checked: the requester, the grantor's authority, both roles,
  the epoch, the hierarchy relationship, and the requester's eligibility.
- Exactly one commander: a second request at the old epoch is stale once the
  first is granted. Only the two agents directly involved update their view
  (the new top, and the old top that now sits one level below it); deeper
  levels are not notified.
- Anything carrying an older epoch is rejected as stale and logged. A
  commander steps down to follower, and never takes the role back on its
  own, when a newer commander orders it or its own orders are refused as
  stale. A newer epoch reported from below (a failover further down) is
  adopted without stepping down.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Mapping

from src.agents.hierarchy import Hierarchy
from src.roles.failover import choose_successor, succession_candidates
from src.state.suitability import SuitabilityFailure, check_suitability
from src.transport import Message

SCHEMA_VERSION = 1
KEEP = 256
# The fields an upward status heartbeat may carry (the schema's status_snapshot).
STATUS_FIELDS = frozenset(
    {
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

Validator = Callable[[Message], tuple[bool, list[str]]]
# Returns a deny reason, or None when the agent is fit to command.
Eligibility = Callable[[Mapping[str, object]], "str | None"]
# Receives role_change and event records in the exchange-log format.
RecordSink = Callable[[dict], None]


class Role(str, Enum):
    COMMANDER = "commander"
    FOLLOWER = "follower"
    TRANSITIONING = "transitioning"


class DenyReason(str, Enum):
    BUSY = "busy"
    DEGRADED = "degraded"
    TERRAIN_UNSUITABLE = "terrain_unsuitable"
    EQUIPMENT_MISSING = "equipment_missing"
    NOT_AUTHORIZED = "not_authorized"


class RoleError(Exception):
    """A role action that can't be taken; nothing was sent."""


def basic_eligibility(
    *,
    min_battery: int = 20,
    required_capabilities: tuple[str, ...] = (),
    unsuitable_terrain: tuple[str, ...] = (),
) -> Eligibility:
    """Build the commander eligibility check used for grants and succession.

    Return None for an eligible snapshot or the first role denial reason.
    Battery below the threshold or a mechanical fault is degraded, followed
    by busy, unsuitable terrain, and missing equipment.
    """

    blocked_terrain = frozenset(unsuitable_terrain)
    equipment = frozenset(required_capabilities)

    def check(status: Mapping[str, object]) -> str | None:
        battery = status.get("battery_percent", 100)
        if status.get("mechanical_fault") or (
            isinstance(battery, (int, float)) and battery < min_battery
        ):
            return DenyReason.DEGRADED.value
        if status.get("current_command") is not None:
            return DenyReason.BUSY.value
        failure = check_suitability(
            status.get("terrain"),
            status.get("capabilities") or (),
            unsuitable_terrain=blocked_terrain,
            required_capabilities=equipment,
        )
        if failure is SuitabilityFailure.TERRAIN:
            return DenyReason.TERRAIN_UNSUITABLE.value
        if failure is SuitabilityFailure.EQUIPMENT:
            return DenyReason.EQUIPMENT_MISSING.value
        return None

    return check


@dataclass(frozen=True)
class PendingRequest:
    message_id: str
    target_id: str
    epoch: int


class RoleManager:
    """One agent's view of authority. Not thread-safe; use from one thread."""

    def __init__(
        self,
        agent_id: str,
        hierarchy: Hierarchy,
        *,
        validator: Validator | None = None,
        eligibility: Eligibility | None = None,
        status: Callable[[], Mapping[str, object]] | None = None,
        on_record: RecordSink | None = None,
        session_id: str = "manual",
        clock: Callable[[], str] | None = None,
    ) -> None:
        if agent_id not in hierarchy:
            raise RoleError(f"{agent_id!r} is not in the hierarchy")
        if validator is None:
            from src.schema.validator import validate as validator
        self.agent_id = agent_id
        self.hierarchy = hierarchy
        self._validate = validator
        self._eligibility = eligibility or basic_eligibility()
        self._status = status or (lambda: {})
        self._on_record = on_record
        self._record_sinks: list[RecordSink] = []
        self._session_id = session_id
        self._clock = clock or _utc_now
        self._pending: PendingRequest | None = None
        # Set when this agent stepped down without learning who commands now.
        self._awaiting_commander = False
        # The latest status heard from each peer (for example from heartbeats).
        self.peer_status: dict[str, Mapping[str, object]] = {}
        self.records: deque[dict] = deque(maxlen=KEEP)
        self.ignored: deque[tuple[Message, str]] = deque(maxlen=KEEP)
        self.last_deny: str | None = None
        # Failover in progress: the lost commander, who is expected to take
        # over, and the candidates already ruled out.
        self._lost: str | None = None
        self._expected: str | None = None
        self._excluded: set[str] = set()
        self.vacant = False

    # --- what the agent knows ------------------------------------------------

    @property
    def role_epoch(self) -> int:
        return self.hierarchy.role_epoch

    @property
    def role(self) -> Role:
        if self._pending is not None:
            return Role.TRANSITIONING
        if self._awaiting_commander:
            return Role.FOLLOWER
        if self.hierarchy.is_top(self.agent_id):
            return Role.COMMANDER
        return Role.FOLLOWER

    def get_current_commander(self) -> str | None:
        """This agent's commander; None at the top, if unknown, or if vacant."""
        if self._awaiting_commander or self.vacant:
            return None
        return self.hierarchy.parent_of(self.agent_id)

    def followers(self) -> tuple[str, ...]:
        return self.hierarchy.children_of(self.agent_id)

    def known_peers(self) -> dict[str, str]:
        """Each direct peer and its role relative to this agent."""
        peers = {f: Role.FOLLOWER.value for f in self.followers()}
        commander = self.get_current_commander()
        if commander is not None:
            peers[commander] = Role.COMMANDER.value
        return peers

    def add_record_sink(self, sink: RecordSink) -> None:
        """Attach a runtime persistence sink without replacing existing observers."""
        if not callable(sink):
            raise TypeError("Record sink must be callable")
        if sink != self._on_record and sink not in self._record_sinks:
            self._record_sinks.append(sink)

    def remove_record_sink(self, sink: RecordSink) -> None:
        """Detach a runtime-owned sink when that runtime stops."""
        if sink in self._record_sinks:
            self._record_sinks.remove(sink)

    def observe_status(self, agent_id: str, status: Mapping[str, object]) -> None:
        """Remember an agent's latest shared status, used to check eligibility.

        Feed every status from the commander's heartbeat, this agent's own
        included, so that every survivor decides failover on the same data.
        """
        self.peer_status[agent_id] = dict(status)

    # --- failover ------------------------------------------------------------

    @property
    def expected_successor(self) -> str | None:
        """Who this agent expects to take over, while a failover is running."""
        return self._expected

    def on_commander_lost(self, failed_id: str | None = None) -> list[Message]:
        """The commander is confirmed gone; apply the failover rule.

        Every survivor calls this (failure detection is the runtime's job).
        If this agent is the designated successor it re-checks itself live:
        if fit, it promotes itself and returns announcements for its new
        followers; if not, it declines and the next candidate is expected.
        """
        failed = failed_id or self.get_current_commander()
        if failed is None or failed not in self.hierarchy:
            raise RoleError(f"{self.agent_id} has no commander to lose")
        if self.agent_id not in self.hierarchy.subtree(failed)[1:]:
            raise RoleError(f"{self.agent_id} is not below {failed!r}")
        self._lost = failed
        self._excluded = set()
        self.vacant = False
        return self._next_candidate()

    @property
    def awaited_successor(self) -> str | None:
        """During a failover, the other agent expected to announce itself."""
        if self._lost is None or self._expected == self.agent_id:
            return None
        return self._expected

    @property
    def declined_promotion(self) -> bool:
        """Whether this agent declined its own promotion in the active failover."""
        return self.agent_id in self._excluded

    def candidate_timed_out(self) -> list[Message]:
        """The expected successor never announced itself; move to the next one."""
        if self._lost is None or self._expected is None:
            return []
        self._excluded.add(self._expected)
        return self._next_candidate()

    def _next_candidate(self) -> list[Message]:
        self._expected = choose_successor(
            self.hierarchy,
            self._lost,
            statuses=self.peer_status,
            eligibility=self._eligibility,
            excluded=self._excluded,
        )
        if self._expected is None:
            self.vacant = True
            self._record_change(
                self.agent_id, "vacant", self.role_epoch, None, None, self._lost
            )
            return []
        if self._expected == self.agent_id:
            return self._try_promote()
        return []

    def _try_promote(self) -> list[Message]:
        failed = self._lost
        unfit = self._eligibility(self._status())  # live re-check
        if unfit is not None:
            self._record_change(
                self.agent_id,
                "promotion_declined",
                self.role_epoch,
                None,
                unfit,
                failed,
            )
            self._excluded.add(self.agent_id)
            return self._next_candidate()

        old_epoch = self.role_epoch
        self.hierarchy = self.hierarchy.after_failure(
            failed, self.agent_id, role_epoch=old_epoch + 1
        )
        self._end_failover()
        self._record_change(
            self.agent_id,
            "self_promoted",
            old_epoch,
            self.role_epoch,
            None,
            failed,
            target=self.agent_id,
            new_role=Role.COMMANDER,
        )
        announcements = [
            self._envelope(
                follower, "heartbeat", Role.COMMANDER, {"status_summary": []}
            )
            for follower in self.followers()
            if follower != failed
        ]
        report = self.status_report()
        if report is not None:
            announcements.append(report)
        return announcements

    def status_report(
        self, status: Mapping[str, object] | None = None
    ) -> Message | None:
        """An upward heartbeat with this agent's own status, at its epoch.

        Used to tell the commander above about a new epoch straight away.
        ``status`` defaults to the agent's status source. None at the top, or
        when the status can't make a valid heartbeat.
        """
        parent = self.get_current_commander()
        if parent is None:
            return None
        source = self._status() if status is None else status
        status = {key: value for key, value in source.items() if key in STATUS_FIELDS}
        status["role_epoch"] = self.role_epoch
        status["agent_id"] = self.agent_id
        try:
            return self._envelope(
                parent, "heartbeat", Role.FOLLOWER, {"status": status}
            )
        except RoleError:
            return None

    def _end_failover(self) -> None:
        self._lost = None
        self._expected = None
        self._excluded = set()
        self.vacant = False

    # --- asking for the role -------------------------------------------------

    def request_role(self, target_id: str | None = None) -> Message:
        """Build a ``role_request``. Defaults to asking the top directly.

        Raises ``RoleError`` when the request makes no sense or this agent
        fails its own eligibility self-check; nothing is sent then.
        """
        if self._pending is not None:
            raise RoleError("a role request is already waiting for an answer")
        if self.role is Role.COMMANDER:
            raise RoleError(f"{self.agent_id} is already the commander")
        if self._awaiting_commander:
            raise RoleError(f"{self.agent_id} doesn't know who commands now")
        target_id = target_id or self.hierarchy.top
        if target_id not in self.hierarchy.path_to_top(self.agent_id)[1:]:
            raise RoleError(f"{target_id!r} is not a commander above {self.agent_id}")
        unfit = self._eligibility(self._status())
        if unfit is not None:
            raise RoleError(f"{self.agent_id} fails its own eligibility check: {unfit}")

        message = self._envelope(
            target_id,
            "role_request",
            Role.FOLLOWER,
            {"target_id": target_id, "requested_role": "commander"},
        )
        self._pending = PendingRequest(
            message["message_id"], target_id, self.role_epoch
        )
        return message

    def cancel_request(self) -> None:
        """Give up waiting (for example after a timeout); a late answer is ignored."""
        self._pending = None

    # --- handling role messages ----------------------------------------------

    def handle(self, message: Message) -> list[Message]:
        """Handle one received message; return the messages to send back."""
        kind = message.get("type")
        if kind in ("role_request", "role_grant", "role_deny"):
            ok, problems = self._validate(message)
            if not ok:
                self.ignored.append((message, "; ".join(problems)))
                return []
        if kind == "role_request":
            return self._on_request(message)
        if kind in ("role_grant", "role_deny"):
            self._on_answer(message)
            return []
        self.observe(message)
        return []

    def observe(self, message: Message) -> bool:
        """Check a non-role message's epoch. Returns False if it is stale.

        An older epoch is logged as a stale rejection. A newer epoch means
        authority moved on: this agent adopts it, and if it thought it was the
        commander it steps down (it never takes the role back by itself).
        """
        epoch = message.get("role_epoch")
        if type(epoch) is not int:
            return True
        if epoch < self.role_epoch:
            self._stale(message)
            return False
        if epoch > self.role_epoch:
            if self._accept_report_from_below(message, epoch):
                return True
            if not self._accept_newer_authority(message, epoch):
                return False
            self._adopt_newer_epoch(message, epoch)
        elif self._awaiting_commander and self._is_commanding_me(message):
            self.hierarchy = self._with_top(message["sender_id"], epoch)
            self._awaiting_commander = False
        return True

    # --- internals: grantor side ---------------------------------------------

    def _on_request(self, request: Message) -> list[Message]:
        requester = request.get("sender_id")
        epoch = request.get("role_epoch")
        if type(epoch) is int and epoch < self.role_epoch:
            self._stale(request)
            return [self._stale_error(request)]

        reason = self._deny_reason(request)
        old_epoch = self.role_epoch
        if reason is not None:
            self._record_change(requester, "denied", old_epoch, None, reason)
            return [
                self._envelope(
                    requester,
                    "role_deny",
                    Role.COMMANDER,
                    {"in_reply_to": request["message_id"], "reason": reason},
                )
            ]

        new_epoch = old_epoch + 1
        previous = self.agent_id
        self.hierarchy = self.hierarchy.with_new_top(requester, role_epoch=new_epoch)
        self._record_change(requester, "granted", old_epoch, new_epoch, None, previous)
        grant = self._envelope(
            requester,
            "role_grant",
            Role.COMMANDER,
            {
                "in_reply_to": request["message_id"],
                "granted": True,
                "new_epoch": new_epoch,
            },
            role_epoch=old_epoch,
        )
        return [grant]

    def _deny_reason(self, request: Message) -> str | None:
        requester = request.get("sender_id")
        payload = request.get("payload")
        epoch = request.get("role_epoch")
        if not isinstance(payload, dict) or payload.get("target_id") != self.agent_id:
            return DenyReason.NOT_AUTHORIZED.value
        if self._pending is not None:
            return DenyReason.BUSY.value
        if self.role is not Role.COMMANDER or epoch != self.role_epoch:
            # Only the current top, at the current epoch, can hand over the top.
            return DenyReason.NOT_AUTHORIZED.value
        if (
            not isinstance(requester, str)
            or requester == self.agent_id
            or requester not in self.hierarchy
        ):
            return DenyReason.NOT_AUTHORIZED.value
        if requester in self.peer_status:
            return self._eligibility(self.peer_status[requester])
        return None

    def _stale_error(self, request: Message) -> Message:
        return self._envelope(
            request["sender_id"],
            "response",
            Role.COMMANDER,
            {
                "response": "ERROR",
                "in_reply_to": request["message_id"],
                "error": {
                    "code": "STALE_EPOCH",
                    "message": (
                        f"role_epoch {request.get('role_epoch')} is older than"
                        f" {self.role_epoch}"
                    ),
                },
            },
        )

    # --- internals: requester side -------------------------------------------

    def _on_answer(self, answer: Message) -> None:
        pending = self._pending
        payload = answer.get("payload")
        if (
            pending is None
            or not isinstance(payload, dict)
            or payload.get("in_reply_to") != pending.message_id
            or answer.get("sender_id") != pending.target_id
        ):
            self.ignored.append((answer, "no matching role request"))
            return

        self._pending = None
        if answer["type"] == "role_deny":
            self.last_deny = payload.get("reason")
            self._record_change(
                self.agent_id,
                "denied",
                pending.epoch,
                None,
                self.last_deny,
                pending.target_id,
            )
            return

        new_epoch = payload.get("new_epoch")
        if type(new_epoch) is not int or new_epoch <= self.role_epoch:
            self.ignored.append((answer, "grant does not carry a newer epoch"))
            return
        previous = self.hierarchy.top
        self.hierarchy = self.hierarchy.with_new_top(
            self.agent_id, role_epoch=new_epoch
        )
        self._record_change(
            self.agent_id,
            "granted",
            pending.epoch,
            new_epoch,
            None,
            previous,
            target=pending.target_id,
        )

    # --- internals: epochs ---------------------------------------------------

    def _stale(self, message: Message) -> None:
        self._record(
            {
                "record_type": "event",
                "severity": "warning",
                "event_type": "STALE_EPOCH_REJECTED",
                "detail": {
                    "message_id": message.get("message_id"),
                    "sender_id": message.get("sender_id"),
                    "type": message.get("type"),
                    "epoch": message.get("role_epoch"),
                    "current_epoch": self.role_epoch,
                },
            }
        )

    def _accept_report_from_below(self, message: Message, epoch: int) -> bool:
        """A newer epoch reported by an agent this one commands.

        That means a failover happened further down: this agent keeps its
        place and adopts the epoch. If the sender is taking over from one of
        this agent's direct followers, the chain is updated: the sender now
        reports here directly. A ``STALE_EPOCH`` error is not a report: it
        means this agent's own orders are outdated (it was replaced).
        """
        sender = message.get("sender_id")
        if (
            message.get("sender_role") != Role.FOLLOWER.value
            or message.get("recipient_id") != self.agent_id
            or not isinstance(sender, str)
            or sender not in self.hierarchy
            or sender == self.agent_id
            or self.agent_id not in self.hierarchy.path_to_top(sender)[1:]
            or _error_code(message) == "STALE_EPOCH"
            or self._awaiting_commander
        ):
            return False
        old_epoch = self.role_epoch
        path = self.hierarchy.path_to_top(sender)
        # This agent's direct follower on the way up from the sender.
        failed = path[path.index(self.agent_id) - 1]
        if sender == failed or sender not in succession_candidates(
            self.hierarchy, failed
        ):
            # Just a newer epoch climbing up the chain.
            self.hierarchy = Hierarchy(_parents(self.hierarchy), role_epoch=epoch)
            return True
        self.hierarchy = self.hierarchy.after_failure(failed, sender, role_epoch=epoch)
        self._record_change(
            sender,
            "successor_accepted",
            old_epoch,
            epoch,
            None,
            failed,
            new_role=Role.COMMANDER,
        )
        return True

    def _accept_newer_authority(self, message: Message, epoch: int) -> bool:
        """Decide whether a newer-epoch order from a commander is legitimate.

        From this agent's own commander: yes. From the agent the failover
        rule designates to replace that commander: yes, and the chain is
        updated. From anyone else: an out-of-turn claim, refused and recorded.
        A former top always accepts (it steps down instead).
        """
        if self.role is Role.COMMANDER or not self._is_commanding_me(message):
            return True
        sender = message["sender_id"]
        parent = self.hierarchy.parent_of(self.agent_id)
        if sender == parent and (self._lost is None or sender != self._expected):
            return True
        failed = self._lost or parent
        if self._lost is not None:
            designated = self._expected
        elif failed is not None:
            designated = choose_successor(
                self.hierarchy,
                failed,
                statuses=self.peer_status,
                eligibility=self._eligibility,
            )
        else:
            designated = None
        if failed is not None and sender == designated:
            self.hierarchy = self.hierarchy.after_failure(
                failed, sender, role_epoch=epoch
            )
            self._end_failover()
            return True
        self._record(
            {
                "record_type": "event",
                "severity": "warning",
                "event_type": "UNAUTHORIZED_PROMOTION",
                "detail": {
                    "message_id": message.get("message_id"),
                    "sender_id": sender,
                    "epoch": epoch,
                    "expected": designated,
                },
            }
        )
        return False

    def _adopt_newer_epoch(self, message: Message, epoch: int) -> None:
        """Authority moved on. Only a former top restructures its view here.

        Anyone else just adopts the epoch: parent pointers change through
        grants, not through epochs. A former top that is now being commanded
        puts that commander on top; without one it waits to learn who commands.
        """
        was_commander = self.role is Role.COMMANDER
        old_epoch = self.role_epoch
        self._pending = None
        if was_commander and self._is_commanding_me(message):
            self.hierarchy = self._with_top(message["sender_id"], epoch)
        else:
            self.hierarchy = Hierarchy(_parents(self.hierarchy), role_epoch=epoch)
            if was_commander:
                self._awaiting_commander = True
        if was_commander:
            self._record_change(
                self.agent_id,
                "stepped_down",
                old_epoch,
                epoch,
                None,
                self.agent_id,
                previous_role=Role.COMMANDER,
                new_role=Role.FOLLOWER,
            )

    def _is_commanding_me(self, message: Message) -> bool:
        """A command or heartbeat sent to this agent by a known commander."""
        sender = message.get("sender_id")
        return (
            message.get("type") in ("command", "heartbeat")
            and message.get("sender_role") == "commander"
            and message.get("recipient_id") == self.agent_id
            and isinstance(sender, str)
            and sender in self.hierarchy
            and sender != self.agent_id
        )

    def _with_top(self, agent_id: str, epoch: int) -> Hierarchy:
        """The chain with ``agent_id`` on top and the old top nested under it."""
        parents = _parents(self.hierarchy)
        old_top = self.hierarchy.top
        if old_top != agent_id:
            parents[agent_id] = None
            parents[old_top] = agent_id
        return Hierarchy(parents, role_epoch=epoch)

    # --- internals: building -------------------------------------------------

    def _envelope(
        self,
        recipient: str,
        kind: str,
        role: Role,
        payload: dict,
        *,
        role_epoch: int | None = None,
    ) -> Message:
        message = {
            "message_id": str(uuid.uuid4()),
            "sender_id": self.agent_id,
            "recipient_id": recipient,
            "timestamp": self._clock(),
            "schema_version": SCHEMA_VERSION,
            "sender_role": role.value,
            "role_epoch": self.role_epoch if role_epoch is None else role_epoch,
            "type": kind,
            "payload": payload,
        }
        ok, errors = self._validate(message)
        if not ok:
            raise RoleError("built an invalid role message: " + "; ".join(errors))
        return message

    def _record_change(
        self,
        requester: str,
        outcome: str,
        old_epoch: int,
        new_epoch: int | None,
        reason: str | None,
        previous_commander: str | None = None,
        *,
        target: str | None = None,
        previous_role: Role = Role.FOLLOWER,
        new_role: Role | None = None,
    ) -> None:
        granted = outcome == "granted"
        self._record(
            {
                "record_type": "role_change",
                "requester_id": requester,
                "target_commander_id": target or self.agent_id,
                "previous_commander_id": previous_commander,
                "previous_role": previous_role.value,
                "new_role": (
                    new_role or (Role.COMMANDER if granted else previous_role)
                ).value,
                "old_epoch": old_epoch,
                "new_epoch": new_epoch,
                "outcome": outcome,
                "reason": reason,
            }
        )

    def _record(self, record: dict) -> None:
        record = {
            **record,
            "session_id": self._session_id,
            "logged_by": self.agent_id,
            "logged_at": self._clock(),
            "hierarchy_level": self.hierarchy.level_of(self.agent_id),
        }
        self.records.append(record)
        if self._on_record is not None:
            self._on_record(record)
        for sink in tuple(self._record_sinks):
            sink(record)


def _parents(hierarchy: Hierarchy) -> dict[str, str | None]:
    return {agent: hierarchy.parent_of(agent) for agent in hierarchy.agents}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _error_code(message: Message) -> str | None:
    payload = message.get("payload")
    if message.get("type") != "response" or not isinstance(payload, dict):
        return None
    error = payload.get("error")
    return error.get("code") if isinstance(error, dict) else None
