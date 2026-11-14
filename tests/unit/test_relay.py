"""How a middle agent summarizes its followers' answers for its commander."""

import pytest

from src.agents.commander import CommandResult, Commander, Outcome
from src.agents.relay import Relay, RelayingDecision, summarize
from src.decision import DecisionResult, DeclineReason
from src.transport import TransportClosedError


def result(outcome, reason=None):
    response = None
    if reason is not None:
        response = {"payload": {"response": "DECLINE", "reason": reason}}
    return CommandResult({"message_id": "m"}, outcome, response)


def relay(**results):
    return Relay("HOLD", {}, results)


def test_all_complied():
    summary = summarize(relay(a=result(Outcome.COMPLY), b=result(Outcome.COMPLY)))
    assert summary == DecisionResult(True)


def test_first_decline_by_follower_id_wins():
    summary = summarize(
        relay(
            c=result(Outcome.DECLINE, "BUSY"),
            a=result(Outcome.COMPLY),
            b=result(Outcome.DECLINE, "BATTERY_CRITICAL"),
        )
    )
    assert summary == DecisionResult(False, DeclineReason.BATTERY_CRITICAL)


@pytest.mark.parametrize(
    "bad",
    [
        result(Outcome.TIMEOUT),
        result(Outcome.INVALID_RESPONSE),
        result(Outcome.ERROR),
        TransportClosedError("gone"),
    ],
    ids=["timeout", "invalid", "error", "link-failed"],
)
def test_any_unanswered_follower_means_no_summary(bad):
    r = relay(a=result(Outcome.DECLINE, "BUSY"), b=bad)
    assert summarize(r) is None
    assert list(r.unanswered()) == ["b"]


def test_outcome_names():
    r = relay(a=result(Outcome.COMPLY), b=TransportClosedError("gone"))
    assert r.outcome_of("a") == "COMPLY"
    assert r.outcome_of("b") == "LINK_FAILED"


def test_relay_timeout_must_be_positive():
    commander = Commander("x", validator=lambda m: (True, []))
    with pytest.raises(ValueError):
        RelayingDecision(commander, timeout=0)


def test_no_followers_means_own_decision_only():
    commander = Commander("x", validator=lambda m: (True, []))
    decide = RelayingDecision(
        commander, timeout=1, own_decision=lambda *_: DecisionResult(True)
    )
    assert decide(None, "HOLD", {}, 0) == DecisionResult(True)
    assert decide.last_relay is None


def error_result(code, agent=None):
    error = {"code": code}
    if agent:
        error["agent"] = agent
    response = {"payload": {"response": "ERROR", "error": error}}
    return CommandResult({"message_id": "m"}, Outcome.ERROR, response)


def test_silent_agent_is_the_first_unanswered_follower():
    r = relay(
        a=result(Outcome.COMPLY),
        c=result(Outcome.TIMEOUT),
        b=TransportClosedError("gone"),
    )
    assert r.silent_agent() == "b"


def test_silent_agent_passes_up_the_agent_a_middle_agent_named():
    r = relay(a=error_result("NO_RESPONSE", "deep-drone"), b=result(Outcome.COMPLY))
    assert r.silent_agent() == "deep-drone"


def test_other_errors_name_the_follower_itself():
    r = relay(a=error_result("STALE_EPOCH"))
    assert r.silent_agent() == "a"


def test_everyone_answered_means_no_silent_agent():
    assert relay(a=result(Outcome.COMPLY)).silent_agent() is None
