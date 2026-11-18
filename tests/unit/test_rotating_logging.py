"""Buffered shutdown, whole-record byte rotation, retention and concurrent writers."""

from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from src.agents.hierarchy import Hierarchy
from src.logging.exchange import ExchangeLogger, ExchangeLogError


def records(directory):
    rows = []
    for path in directory.glob("*.jsonl*"):
        data = path.read_bytes()
        assert not data or data.endswith(b"\n")
        rows += [json.loads(line) for line in data.splitlines()]
    return rows


def test_buffered_records_flush_on_close_and_context_exit(tmp_path):
    with ExchangeLogger(tmp_path, "agent", flush_every=10) as logger:
        logger.record("sent", {"type": "command", "n": 1})
        logger.record("sent", {"type": "command", "n": 2})
        assert logger.path.read_bytes() == b""
    assert [r["message"]["n"] for r in records(tmp_path)] == [1, 2]
    logger.close()
    with pytest.raises(ExchangeLogError):
        logger.record("sent", {})


def test_rotation_counts_utf8_bytes_and_loses_no_retained_record(tmp_path):
    with ExchangeLogger(tmp_path, "agent", max_bytes=900, flush_every=10) as logger:
        for index in range(12):
            logger.record("sent", {"type": "command", "n": index, "text": "é🦅" * 20})
    rows = records(tmp_path)
    assert sorted(row["message"]["n"] for row in rows) == list(range(12))
    assert len(list(tmp_path.glob("*.jsonl*"))) > 1
    assert all(path.stat().st_size <= 900 for path in tmp_path.glob("*.jsonl*"))


def test_keeps_ten_archives_and_one_active_file(tmp_path):
    with ExchangeLogger(tmp_path, "agent", max_bytes=1) as logger:
        assert logger.backup_count == 10
        for index in range(12):
            logger.record("sent", {"type": "command", "n": index})
    assert {path.name for path in tmp_path.iterdir()} == {
        "agent.jsonl",
        *(f"agent.jsonl.{index}" for index in range(1, 11)),
    }
    assert sorted(row["message"]["n"] for row in records(tmp_path)) == list(
        range(1, 12)
    )


def test_concurrent_appends_and_rotation_are_complete(tmp_path):
    with ExchangeLogger(
        tmp_path, "agent", max_bytes=10000, backup_count=100, flush_every=7
    ) as logger:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(
                pool.map(
                    lambda n: logger.record("sent", {"type": "command", "n": n}),
                    range(150),
                )
            )
    assert sorted(r["message"]["n"] for r in records(tmp_path)) == list(range(150))


def test_restart_appends_and_rotates_existing_active_file(tmp_path):
    for index in range(3):
        with ExchangeLogger(tmp_path, "agent", max_bytes=1) as logger:
            logger.record("sent", {"n": index})
    assert sorted(r["message"]["n"] for r in records(tmp_path)) == [0, 1, 2]


def test_levels_follow_skip_level_insertion_and_unknown_sender_is_flagged(tmp_path):
    view = [Hierarchy.chain(["ground", "middle", "leaf"])]
    with ExchangeLogger(tmp_path, "ground", hierarchy=lambda: view[0]) as logger:
        logger.record("sent", {"sender_id": "ground"})
        view[0] = view[0].with_new_top("leaf", role_epoch=1)
        logger.record("sent", {"sender_id": "ground"})
        logger.record("received", {"sender_id": "leaf"})
        logger.record("received", {"sender_id": "unknown"})
        logger.event("TEST_EVENT", {"epoch": 1})
    rows = records(tmp_path)
    assert [row["hierarchy_level"] for row in rows] == [0, 1, 0, None, 1]
    assert rows[3]["unknown_sender"] is True
    assert "unknown_sender" not in rows[2]
    assert all("hierarchy_level" not in r.get("message", {}) for r in rows)


@pytest.mark.parametrize(
    "option,value",
    [
        ("max_bytes", 0),
        ("backup_count", 0),
        ("flush_every", True),
        ("buffer_bytes", -1),
    ],
)
def test_bad_writer_configuration(tmp_path, option, value):
    with pytest.raises(ValueError):
        ExchangeLogger(tmp_path, "agent", **{option: value})


def test_invalid_sender_type_can_still_be_logged_for_validation(tmp_path):
    with ExchangeLogger(tmp_path, "agent", peer_levels={"known": 0}) as logger:
        logger.record("received", {"sender_id": ["invalid"], "type": "command"})
    row = records(tmp_path)[0]
    assert row["hierarchy_level"] is None
    assert row["unknown_sender"] is True
