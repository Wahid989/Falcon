"""Deterministic heartbeat deadlines and the actual RoleManager handoff."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from src.agents.heartbeat import HeartbeatMonitor
from src.agents.hierarchy import Hierarchy
from src.roles.manager import Role, RoleManager
from src.schema.validator import validate

CHAIN = Hierarchy({"ground": None, "agent-a": "ground", "agent-b": "ground"})
FIT = {"battery_percent": 90, "current_command": None}


def monitor(agent="agent-b"):
    now = [0.0]
    roles = RoleManager(agent, CHAIN, status=lambda: FIT)
    roles.on_commander_lost = Mock(wraps=roles.on_commander_lost)
    roles.observe_status = Mock(wraps=roles.observe_status)
    held = Mock()
    beat = HeartbeatMonitor(
        roles, lambda: FIT, validate, clock=lambda: now[0], on_lost=held
    )
    return beat, roles, now, held


def heartbeat(sender="ground", recipient="agent-b", epoch=0, payload=None):
    return {
        "message_id": "beat-1",
        "sender_id": sender,
        "recipient_id": recipient,
        "timestamp": "2026-11-17T12:00:00Z",
        "schema_version": 1,
        "sender_role": "commander" if sender == "ground" else "follower",
        "role_epoch": epoch,
        "type": "heartbeat",
        "payload": (
            payload
            if payload is not None
            else {
                "status_summary": [
                    {"agent_id": "agent-a", "battery_percent": 5},
                    {"agent_id": "agent-b", "battery_percent": 90},
                ]
            }
        ),
    }


def test_statuses_reach_role_manager_before_three_missed_intervals():
    beat, roles, now, held = monitor()
    message = heartbeat()
    assert beat.received(message, "ground")
    message["payload"]["status_summary"][0]["battery_percent"] = 100
    assert roles.peer_status["agent-a"]["battery_percent"] == 5
    for instant in (5, 10, 14.999):
        now[0] = instant
        beat.poll()
        roles.on_commander_lost.assert_not_called()
    now[0] = 15
    announcements = beat.poll()
    roles.on_commander_lost.assert_called_once_with("ground")
    held.assert_called_once()
    assert roles.role is Role.COMMANDER  # a is unfit, so b wins.
    assert roles.role_epoch == 1
    assert announcements
    assert all(validate(message)[0] for message in announcements)
    now[0] = 30
    beat.poll()
    roles.on_commander_lost.assert_called_once()


def test_normal_parent_message_refreshes_liveness():
    beat, roles, now, _ = monitor()
    now[0] = 10
    command = heartbeat()
    command.update(type="command", payload={"command": "HOLD"})
    assert beat.received(command, "ground")
    now[0] = 24.999
    beat.poll()
    roles.on_commander_lost.assert_not_called()
    now[0] = 25
    beat.poll()
    roles.on_commander_lost.assert_called_once_with("ground")


@pytest.mark.parametrize(
    "mutation,peer",
    [
        ({"role_epoch": 1}, "ground"),
        ({"role_epoch": -1}, "ground"),
        ({"sender_id": "intruder"}, "ground"),
        ({"recipient_id": "agent-a"}, "ground"),
        ({"sender_role": "follower"}, "ground"),
        ({}, "agent-a"),
        ({"payload": {"status": FIT}}, "ground"),
    ],
)
def test_invalid_or_untrusted_heartbeat_never_refreshes_timer(mutation, peer):
    beat, roles, now, _ = monitor()
    message = deepcopy(heartbeat())
    message.update(mutation)
    now[0] = 14
    assert not beat.received(message, peer)
    roles.observe_status.assert_not_called()
    now[0] = 15
    beat.poll()
    roles.on_commander_lost.assert_called_once_with("ground")


def test_idle_commander_heartbeat_shares_all_known_child_statuses():
    beat, roles, now, _ = monitor("ground")
    roles.observe_status("agent-a", {"battery_percent": 5})
    roles.observe_status("agent-b", FIT)
    now[0] = 4.999
    assert beat.poll() == []
    now[0] = 5
    messages = beat.poll()
    assert {message["recipient_id"] for message in messages} == {"agent-a", "agent-b"}
    for message in messages:
        assert validate(message)[0]
        assert {s["agent_id"] for s in message["payload"]["status_summary"]} == {
            "agent-a",
            "agent-b",
        }
        assert "hierarchy_level" not in message
    now[0] = 6
    assert beat.poll() == []


def test_upward_status_updates_cache_without_starving_downward_beats():
    beat, roles, now, _ = monitor("ground")
    now[0] = 4.9
    message = heartbeat(
        "agent-a", "ground", payload={"status": {"agent_id": "agent-a", **FIT}}
    )
    assert beat.received(message, "agent-a")
    assert roles.peer_status["agent-a"]["battery_percent"] == 90
    now[0] = 5
    assert len(beat.poll()) == 2


def test_real_send_suppresses_idle_beat_for_that_peer():
    beat, _, now, _ = monitor("ground")
    now[0] = 4
    beat.sent("agent-a")
    now[0] = 5
    assert [m["recipient_id"] for m in beat.poll()] == ["agent-b"]


@pytest.mark.parametrize(
    "interval,misses",
    [(0, 3), (-1, 3), (float("inf"), 3), (True, 3), (5, 0), (5, True)],
)
def test_invalid_configuration(interval, misses):
    with pytest.raises(ValueError):
        HeartbeatMonitor(
            RoleManager("agent-b", CHAIN),
            lambda: FIT,
            validate,
            interval=interval,
            missed_intervals=misses,
        )
