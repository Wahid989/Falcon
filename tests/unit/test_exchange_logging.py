"""Exchange format, isolation, heartbeat exclusion, and append/error behavior."""

import json

import pytest

from src.logging.exchange import ExchangeLogger, ExchangeLogError


def test_append_and_levels_without_mutating_envelope(tmp_path):
    logger = ExchangeLogger(
        tmp_path, "commander", session_id="run", level=0, peer_levels={"follower-1": 1}
    )
    message = {
        "type": "command",
        "sender_id": "commander",
        "payload": {"command": "HOLD"},
    }
    logger.record("sent", message)
    message["payload"]["command"] = "RETURN"
    logger.record("received", {"type": "response", "sender_id": "follower-1"})
    logger.record("received", {"type": "response", "sender_id": "unknown"})
    lines = logger.path.read_text().splitlines()
    assert len(lines) == 3
    records = [json.loads(line) for line in lines]
    assert records[0]["message"]["payload"] == {"command": "HOLD"}
    assert [r["hierarchy_level"] for r in records] == [0, 1, None]
    assert all(
        r["record_type"] == "message" and r["session_id"] == "run" for r in records
    )
    assert all(
        r["logged_by"] == "commander" and r["logged_at"].endswith("Z") for r in records
    )
    assert "hierarchy_level" not in message
    assert logger.path.read_bytes().endswith(b"\n")


def test_heartbeats_are_not_exchange_records(tmp_path):
    logger = ExchangeLogger(tmp_path, "agent")
    logger.record("sent", {"type": "heartbeat"})
    logger.record("received", {"type": "heartbeat"})
    assert not logger.path.exists()


def test_file_is_appended_across_writer_instances(tmp_path):
    for _ in range(2):
        ExchangeLogger(tmp_path, "agent").record("sent", {"type": "command"})
    assert len((tmp_path / "agent.jsonl").read_text().splitlines()) == 2


def test_writer_errors_are_visible(tmp_path):
    logger = ExchangeLogger(tmp_path, "agent")
    logger.path.mkdir()  # Writing a directory must fail, not silently lose evidence.
    with pytest.raises(ExchangeLogError):
        logger.record("sent", {"type": "command"})


@pytest.mark.parametrize("agent", ["../escape", "", "bad/name"])
def test_invalid_log_identity(tmp_path, agent):
    with pytest.raises(ValueError):
        ExchangeLogger(tmp_path, agent)
