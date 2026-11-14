"""Passing a command down the chain and summarizing the answers upward.

When a middle agent's commander sends it a command, the middle agent first
makes its own decision. If it complies, it passes the same command to each of
its followers, waits for them, and answers its commander with a summary:

- every follower complied: ``COMPLY``;
- a follower declined: ``DECLINE`` with that follower's reason (the first,
  in follower-id order);
- a follower didn't answer properly (timed out, sent an invalid or ``ERROR``
  reply, or its link failed): ``ERROR`` with code ``NO_RESPONSE`` and
  ``error.agent`` naming the silent follower. When that follower is itself a
  middle agent reporting ``NO_RESPONSE``, the agent it named is passed up, so
  the top learns which agent actually went silent.

Followers that complied stay complied even when the summary is ``DECLINE``:
the vocabulary has no undo. ``REPORT_STATUS`` is never relayed; the middle
agent answers with its own status. Set the middle agent's relay timeout
below its commander's response timeout, or the commander gives up first.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Callable, Mapping

from src.agents.commander import CommandResult, Commander, Outcome
from src.decision import DecisionEngine, DecisionResult, DeclineReason, decide
from src.transport import TransportError

RELAYED_COMMANDS = frozenset({"HOLD", "PROCEED_TO_WAYPOINT", "RETURN"})


class RelayIncomplete(Exception):
    """A follower didn't answer properly; the answer upward is NO_RESPONSE."""

    def __init__(self, relay: Relay) -> None:
        self.relay = relay
        self.silent_agent = relay.silent_agent()
        missing = ", ".join(
            f"{fid} ({outcome})" for fid, outcome in relay.unanswered().items()
        )
        super().__init__(f"{relay.command} not confirmed by: {missing}")


@dataclass(frozen=True)
class Relay:
    """One command passed down, and what each follower did with it."""

    command: str
    payload: Mapping[str, object]
    results: Mapping[str, CommandResult | TransportError]

    def outcome_of(self, follower_id: str) -> str:
        result = self.results[follower_id]
        if isinstance(result, TransportError):
            return "LINK_FAILED"
        return result.outcome.value

    def silent_agent(self) -> str | None:
        """The agent to name in NO_RESPONSE: the first unanswered follower by id,
        or the agent that follower itself reported as silent."""
        for follower_id in sorted(self.unanswered()):
            result = self.results[follower_id]
            if (
                isinstance(result, CommandResult)
                and result.outcome is Outcome.ERROR
                and isinstance(result.error, dict)
                and result.error.get("code") == "NO_RESPONSE"
                and isinstance(result.error.get("agent"), str)
            ):
                return result.error["agent"]
            return follower_id
        return None

    def unanswered(self) -> dict[str, str]:
        return {
            fid: self.outcome_of(fid)
            for fid in sorted(self.results)
            if self.outcome_of(fid) not in ("COMPLY", "DECLINE")
        }


def summarize(relay: Relay) -> DecisionResult | None:
    """COMPLY or DECLINE for the commander, or None when a follower didn't
    answer properly (the caller then reports NO_RESPONSE)."""
    if relay.unanswered():
        return None
    for follower_id in sorted(relay.results):
        result = relay.results[follower_id]
        if result.outcome is Outcome.DECLINE:
            return DecisionResult(False, DeclineReason(result.reason))
    return DecisionResult(True)


class RelayingDecision:
    """A decision engine for a middle agent that relays before answering.

    Use it as the middle agent's Follower decision engine. It runs inside the
    Follower module's thread and uses the agent's Commander module from that
    thread, so nothing else should use the Commander at the same time.
    """

    def __init__(
        self,
        commander: Commander,
        *,
        timeout: float,
        own_decision: DecisionEngine = decide,
        on_relay: Callable[[Relay], None] | None = None,
    ) -> None:
        if not timeout > 0:
            raise ValueError("relay timeout must be positive")
        self._commander = commander
        self._timeout = timeout
        self._own = own_decision
        self._on_relay = on_relay
        self.last_relay: Relay | None = None

    def __call__(self, state, command, payload, message_role_epoch) -> DecisionResult:
        own = self._own(state, command, payload, message_role_epoch)
        if (
            not own.complied
            or command not in RELAYED_COMMANDS
            or not self._commander.followers
        ):
            return own

        relay = self._relay(command, payload)
        self.last_relay = relay
        if self._on_relay is not None:
            self._on_relay(relay)
        summary = summarize(relay)
        if summary is None:
            raise RelayIncomplete(relay)
        return summary

    def _relay(self, command: str, payload: Mapping[str, object]) -> Relay:
        waypoint = payload.get("waypoint")
        sent = self._commander.broadcast(command, waypoint=waypoint)
        results: dict[str, CommandResult | TransportError] = dict(sent.failed)
        deadline = monotonic() + self._timeout
        for follower_id, message in sent.sent.items():
            try:
                results[follower_id] = self._commander.await_response(
                    message, max(deadline - monotonic(), 0.0)
                )
            except TransportError as error:
                results[follower_id] = error
        return Relay(command, dict(payload), results)
