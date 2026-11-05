"""Fast live TCP counterpart of the same checkpoint executed inside Compose."""

from concurrent.futures import ThreadPoolExecutor
import json
from unittest.mock import Mock

import pytest

from scripts.mvp_checkpoint import main, run_checkpoint, verify_logs
from src.decision import decide
from src.agents.commander_runner import FollowerAddress
from src.agents.tcp_runtime import FollowerTCPServer
from src.logging.exchange import ExchangeLogger
from src.state import FollowerState


def test_checkpoint_proves_round_trip_broadcast_decline_and_logs(tmp_path):
    session = "test-mvp"
    servers, futures, addresses = [], [], {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        try:
            for name in ("follower-1", "follower-2", "follower-3", "follower-low"):
                low = name == "follower-low"
                state = FollowerState(
                    battery_percent=5 if low else 100,
                    position="north-fence",
                    movement_flag=not low,
                    current_command=None if low else "RETURN",
                )
                logger = ExchangeLogger(
                    tmp_path,
                    name,
                    session_id=session,
                    level=1,
                    peer_levels={"commander": 0},
                )
                server = FollowerTCPServer(
                    name, "commander", decide, state=state, port=0, exchange_log=logger
                )
                servers.append(server)
                host, port = server.listen()
                addresses[name] = FollowerAddress(name, host, port)
                futures.append(executor.submit(server.serve))
            report = run_checkpoint(
                addresses, log_dir=tmp_path, session_id=session, timeout=2
            )
            assert report["decision"] == "GO"
            assert all(report["checks"].values())
            assert report["low_battery_before"] == report["low_battery_after"]
            assert report["evidence"] == {
                "wire_messages": 28,
                "message_log_records": 56,
            }
            assert len(list(tmp_path.glob("*.jsonl"))) == 5
        finally:
            for server in servers:
                server.close()
            for future in futures:
                future.result(timeout=2)


def test_missing_log_evidence_fails(tmp_path):
    message = {
        "message_id": "request",
        "sender_id": "commander",
        "recipient_id": "follower-1",
    }
    with pytest.raises(ValueError, match="sender log"):
        verify_logs(
            tmp_path,
            "run",
            [Mock(command=message, response=dict(message, message_id="reply"))],
        )


def test_checkpoint_failure_writes_no_go_report(tmp_path):
    report = tmp_path / "report.json"
    code = main(
        [
            "--follower",
            "follower-1=127.0.0.1:5000",
            "--log-dir",
            str(tmp_path),
            "--session-id",
            "run",
            "--report",
            str(report),
        ]
    )
    assert code == 1
    assert json.loads(report.read_text())["decision"] == "NO_GO"
