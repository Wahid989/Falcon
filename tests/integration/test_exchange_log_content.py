"""What a real Commander and Follower write to their exchange logs.

Both agents run with the project's JSONL writer, in the record format the
storage service ingests. These tests pin that every wire message appears once
on each side, with the right direction and log-only hierarchy_level, and that
heartbeats stay out. They also check that the fixture the storage tests ingest
still matches what the writer produces today.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.agents import Commander, Follower, Outcome
from src.logging.exchange import ExchangeLogger
from src.schema.validator import validate
from src.state import FollowerState
from src.transport import InMemoryTransport

REPO = Path(__file__).resolve().parents[2]
STORAGE_FIXTURE = (
    REPO / "storage/dotnet/tests/FalconStorage.Tests/Fixtures/agent-logs/session-a"
)
RECORD_KEYS = {
    "record_type",
    "session_id",
    "logged_by",
    "logged_at",
    "direction",
    "hierarchy_level",
    "message",
}


def run_exchange(log_dir, session_id="log-content"):
    """Commander (level 0) and follower-1 (level 1) exchange three commands.

    HOLD and REPORT_STATUS are answered normally; RETURN is declined because
    the follower's battery is low. The follower also sends one heartbeat,
    which must not be logged. Returns the commander's results.
    """
    commander_end, follower_end = InMemoryTransport.pair()
    commander_end.connect()
    follower_end.connect()
    commander = Commander(
        "commander",
        exchange_log=ExchangeLogger(
            log_dir,
            "commander",
            session_id=session_id,
            level=0,
            peer_levels={"follower-1": 1},
        ),
    )
    commander.add_follower("follower-1", commander_end)
    follower = Follower(
        agent_id="follower-1",
        transport=follower_end,
        state=FollowerState(parent_id="commander", battery_percent=5),
        exchange_log=ExchangeLogger(
            log_dir,
            "follower-1",
            session_id=session_id,
            level=1,
            peer_levels={"commander": 0},
        ),
    )

    results = []
    try:
        for command in ("HOLD", "RETURN", "REPORT_STATUS"):
            if command == "REPORT_STATUS":
                follower_end.send(heartbeat(follower))
            with ThreadPoolExecutor(max_workers=1) as pool:
                handled = pool.submit(follower.handle_once, timeout=2.0)
                results.append(commander.issue("follower-1", command, timeout=2.0))
                handled.result(timeout=2.0)
    finally:
        commander_end.close()
        follower_end.close()
    return results


def heartbeat(follower):
    message = {
        "message_id": "00000000-0000-4000-8000-000000000001",
        "sender_id": "follower-1",
        "recipient_id": "commander",
        "timestamp": "2026-11-13T12:00:00.000000Z",
        "schema_version": 1,
        "sender_role": "follower",
        "role_epoch": 0,
        "type": "heartbeat",
        "payload": {"status": follower.status_snapshot()},
    }
    assert validate(message) == (True, [])
    return message


def read_log(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_both_agents_log_every_exchange(tmp_path):
    results = run_exchange(tmp_path)
    assert [r.outcome for r in results] == [
        Outcome.COMPLY,
        Outcome.DECLINE,
        Outcome.STATUS_RESPONSE,
    ]

    commander_log = read_log(tmp_path / "commander.jsonl")
    follower_log = read_log(tmp_path / "follower-1.jsonl")

    # 3 commands and 3 responses, each logged once by each side.
    wire_ids = {r.command["message_id"] for r in results} | {
        r.response["message_id"] for r in results
    }
    assert len(wire_ids) == 6
    for log in (commander_log, follower_log):
        assert len(log) == 6
        assert {r["message"]["message_id"] for r in log} == wire_ids

    # What one side sent, the other received, unchanged.
    sent_by_commander = {
        r["message"]["message_id"]: r["message"]
        for r in commander_log
        if r["direction"] == "sent"
    }
    received_by_follower = {
        r["message"]["message_id"]: r["message"]
        for r in follower_log
        if r["direction"] == "received"
    }
    assert sent_by_commander == received_by_follower


def test_hierarchy_level_is_logged_but_never_on_the_wire(tmp_path):
    run_exchange(tmp_path)

    for record in read_log(tmp_path / "commander.jsonl"):
        # The commander is the top (0); what it receives comes from level 1.
        expected = 0 if record["direction"] == "sent" else 1
        assert record["hierarchy_level"] == expected
        assert "hierarchy_level" not in record["message"]
    for record in read_log(tmp_path / "follower-1.jsonl"):
        expected = 1 if record["direction"] == "sent" else 0
        assert record["hierarchy_level"] == expected
        assert "hierarchy_level" not in record["message"]


def test_log_records_have_the_storage_format(tmp_path):
    run_exchange(tmp_path, session_id="fmt")
    for name in ("commander.jsonl", "follower-1.jsonl"):
        for record in read_log(tmp_path / name):
            assert set(record) == RECORD_KEYS
            assert record["record_type"] == "message"
            assert record["session_id"] == "fmt"
            assert record["direction"] in {"sent", "received"}
            assert record["logged_at"].endswith("Z")
            assert validate(record["message"]) == (True, [])


def test_heartbeats_are_not_logged(tmp_path):
    run_exchange(tmp_path)
    for name in ("commander.jsonl", "follower-1.jsonl"):
        types = {r["message"]["type"] for r in read_log(tmp_path / name)}
        assert types == {"command", "response"}


def test_decline_is_logged_with_its_reason(tmp_path):
    run_exchange(tmp_path)
    declines = [
        r
        for r in read_log(tmp_path / "commander.jsonl")
        if r["message"]["payload"].get("response") == "DECLINE"
    ]
    assert len(declines) == 1
    assert declines[0]["direction"] == "received"
    assert declines[0]["message"]["payload"]["reason"] == "BATTERY_CRITICAL"


def test_storage_fixture_matches_what_the_writer_produces(tmp_path):
    # The .NET storage tests ingest a committed copy of these logs. If the
    # writer's record format changes, this fails until the fixture is redone.
    run_exchange(tmp_path)
    for name in ("commander.jsonl", "follower-1.jsonl"):
        live = read_log(tmp_path / name)
        fixture = read_log(STORAGE_FIXTURE / name)
        assert len(fixture) == len(live)
        for fixed, fresh in zip(fixture, live):
            assert set(fixed) == set(fresh)
            assert set(fixed["message"]) == set(fresh["message"])
            assert set(fixed["message"]["payload"]) == set(fresh["message"]["payload"])
            for key in ("direction", "hierarchy_level", "logged_by", "record_type"):
                assert fixed[key] == fresh[key]
            assert fixed["message"]["type"] == fresh["message"]["type"]
