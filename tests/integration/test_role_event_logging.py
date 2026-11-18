"""Persist role handovers and protocol faults using the real agent integration."""

import json
from time import monotonic, sleep
from unittest.mock import Mock

from src.agents import Commander, Follower
from src.agents.dual_role import DualRoleAgent, exchange_logger_for
from src.agents.hierarchy import Hierarchy
from src.logging.exchange import ExchangeLogger
from src.roles.manager import Role, RoleManager
from src.state import FollowerState
from src.transport import InMemoryTransport


def read(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def pair():
    left, right = InMemoryTransport.pair()
    left.connect()
    right.connect()
    return left, right


def test_real_grant_persists_roles_and_refreshes_logger_levels(tmp_path):
    hierarchy = Hierarchy.chain(["ground", "leaf"])
    down, up = pair()
    callback = Mock()
    top_roles = RoleManager("ground", hierarchy, on_record=callback)
    leaf_roles = RoleManager("leaf", hierarchy)
    top_log = exchange_logger_for(hierarchy, "ground", tmp_path, session_id="roles")
    leaf_log = exchange_logger_for(hierarchy, "leaf", tmp_path, session_id="roles")
    top = DualRoleAgent(
        "ground",
        hierarchy,
        follower_links={"leaf": down},
        roles=top_roles,
        exchange_log=top_log,
    )
    leaf = DualRoleAgent(
        "leaf", hierarchy, parent_link=up, roles=leaf_roles, exchange_log=leaf_log
    )
    try:
        top.start()
        leaf.start()
        leaf.request_role()
        deadline = monotonic() + 2
        while leaf_roles.role is not Role.COMMANDER and monotonic() < deadline:
            sleep(0.005)
        assert leaf_roles.role is Role.COMMANDER
        top_log.record("sent", {"type": "command", "sender_id": "ground"})
        leaf_log.record("sent", {"type": "command", "sender_id": "leaf"})
        top_rows = read(top_log.path)
        leaf_rows = read(leaf_log.path)
        grant = next(r for r in top_rows if r["record_type"] == "role_change")
        assert grant["requester_id"] == "leaf"
        assert grant["outcome"] == "granted"
        assert grant["old_epoch"] == 0 and grant["new_epoch"] == 1
        assert grant["session_id"] == "roles"
        assert grant["hierarchy_level"] == 1
        assert top_rows[-1]["hierarchy_level"] == 1
        assert leaf_rows[-1]["hierarchy_level"] == 0
        callback.assert_called_once()
        assert {
            r["message"]["type"] for r in top_rows if r["record_type"] == "message"
        } >= {"role_request", "role_grant"}
    finally:
        top.stop()
        leaf.stop()
        down.close()
        up.close()
        top_log.close()
        leaf_log.close()


def test_stale_authority_event_is_persisted_at_current_level(tmp_path):
    hierarchy = Hierarchy.chain(["ground", "leaf"], role_epoch=3)
    with ExchangeLogger(tmp_path, "leaf", hierarchy=lambda: hierarchy) as logger:
        roles = RoleManager("leaf", hierarchy)
        roles.add_record_sink(logger.write_record)
        roles.observe(
            {
                "message_id": "old",
                "sender_id": "ground",
                "role_epoch": 2,
                "type": "command",
            }
        )
    row = read(tmp_path / "leaf.jsonl")[0]
    assert row["record_type"] == "event"
    assert row["event_type"] == "STALE_EPOCH_REJECTED"
    assert row["detail"]["epoch"] == 2 and row["detail"]["current_epoch"] == 3
    assert row["hierarchy_level"] == 1


def test_follower_protocol_error_has_persistent_context(tmp_path):
    up, down = pair()
    with ExchangeLogger(tmp_path, "leaf", level=1, peer_levels={"ground": 0}) as logger:
        follower = Follower(
            "leaf",
            down,
            state=FollowerState(parent_id="ground", role_epoch=2),
            exchange_log=logger,
        )
        commander = Commander("ground", role_epoch=1)
        request = commander.build_command("leaf", "HOLD")
        up.send(request)
        follower.handle_once(0.1)
    rows = read(tmp_path / "leaf.jsonl")
    event = next(row for row in rows if row["record_type"] == "event")
    assert event["event_type"] == "PROTOCOL_ERROR"
    assert event["detail"]["in_reply_to"] == request["message_id"]
    assert event["detail"]["error"]["code"] == "STALE_EPOCH"
    assert len([r for r in rows if r["record_type"] == "message"]) == 2
    up.close()
    down.close()
