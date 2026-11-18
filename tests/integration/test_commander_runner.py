"""Commander runner over real TCP against the production Follower policy."""

import copy
import io
import json
import math
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from src.agents import Follower
from src.agents import commander_runner
from src.agents.commander_runner import (
    EXIT_OK,
    EXIT_UNANSWERED,
    EXIT_USAGE,
    main,
    parse_follower,
)
from src.schema.validator import validate
from src.state import CommandLifecycle, FollowerState
from src.transport import TCPServerTransport, TransportError

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMANDER_ID = "commander"
ACCEPTANCE_TIMEOUT_SECONDS = float(os.environ.get("FALCON_ACCEPTANCE_TIMEOUT", "5"))
PEER_CLEANUP_TIMEOUT_SECONDS = max(ACCEPTANCE_TIMEOUT_SECONDS + 5, 10)

if not math.isfinite(ACCEPTANCE_TIMEOUT_SECONDS) or not (
    0 < ACCEPTANCE_TIMEOUT_SECONDS <= 60
):
    raise ValueError("FALCON_ACCEPTANCE_TIMEOUT must be between 0 and 60 seconds")

_FOLLOWERS = []
_SILENT_PEERS = []


@pytest.fixture(autouse=True)
def cleanup_tcp_peers():
    yield
    for peer in _SILENT_PEERS:
        peer.stop()
    _SILENT_PEERS.clear()
    for follower in _FOLLOWERS:
        follower.close()
    _FOLLOWERS.clear()


class FollowerProcess:
    """A Follower answering one command on its own TCP server, in a thread."""

    def __init__(self, agent_id, state=None, *, port=0, start_delay=0.0):
        self.agent_id = agent_id
        self.follower = None
        self._initial_state = state or FollowerState(parent_id=COMMANDER_ID)
        self.server = TCPServerTransport("127.0.0.1", port, connect_timeout=10)
        self.port = port
        self.result = None
        self.error = None
        self._start_delay = start_delay
        self._ready = threading.Event()
        if not start_delay:
            self.port = self.server.listen()[1]
            self._ready.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        _FOLLOWERS.append(self)

    def _run(self):
        try:
            if self._start_delay:
                time.sleep(self._start_delay)
                self.port = self.server.listen()[1]
                self._ready.set()
            self.server.connect()
            self.follower = Follower(
                agent_id=self.agent_id,
                transport=self.server,
                state=self._initial_state,
            )
            self.result = self.follower.handle_once(
                timeout=PEER_CLEANUP_TIMEOUT_SECONDS
            )
        except Exception as exc:  # surfaced through .error
            self.error = exc
        finally:
            self._ready.set()
            self.server.close()

    def wait_ready(self, timeout=PEER_CLEANUP_TIMEOUT_SECONDS):
        """Wait until the TCP listener is bound or its startup has failed."""
        assert self._ready.wait(timeout), "Follower did not become ready in time"
        assert self.error is None, self.error

    @property
    def state(self):
        """The follower's state now (the Follower replaces it on each change)."""
        return self.follower.state

    @property
    def spec(self):
        return f"{self.agent_id}=127.0.0.1:{self.port}"

    def join(self):
        self._thread.join(timeout=PEER_CLEANUP_TIMEOUT_SECONDS)
        assert not self._thread.is_alive()
        assert self.error is None, self.error

    def close(self):
        self.server.close()
        self._thread.join(timeout=PEER_CLEANUP_TIMEOUT_SECONDS)
        assert not self._thread.is_alive(), "Follower thread did not stop"


class SilentPeer:
    """Accepts the connection and reads the command, but never answers."""

    def __init__(self):
        self.server = TCPServerTransport("127.0.0.1", 0, connect_timeout=10)
        self.port = self.server.listen()[1]
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        _SILENT_PEERS.append(self)

    def _run(self):
        try:
            self.server.connect()
            self.server.receive(timeout=10)
            self._done.wait(10)
        except TransportError:
            pass
        finally:
            self.server.close()

    def stop(self):
        self._done.set()
        self.server.close()
        self._thread.join(timeout=PEER_CLEANUP_TIMEOUT_SECONDS)
        assert not self._thread.is_alive(), "Silent peer thread did not stop"


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def run_cli(*args):
    out = io.StringIO()
    code = main([*args], out=out)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    return code, lines[:-1], lines[-1]["summary"]


# --- one follower ---------------------------------------------------------------


def moving():
    return FollowerState(parent_id=COMMANDER_ID, movement_flag=True)


def test_hold_round_trip_completes_within_timeout():
    follower = FollowerProcess("follower-1", moving())
    follower.wait_ready()
    timeout = ACCEPTANCE_TIMEOUT_SECONDS
    code, records, summary = run_cli(
        "--follower",
        follower.spec,
        "--timeout",
        str(timeout),
        "--connect-retries",
        "0",
    )
    follower.join()

    assert code == EXIT_OK
    (record,) = records
    assert record["follower"] == "follower-1"
    assert record["command"] == "HOLD"
    assert record["outcome"] == "COMPLY"
    assert record["latency_ms"] < timeout * 1000
    assert summary == {"command": "HOLD", "followers": 1, "answered": 1}
    # The follower saw the same command the commander reports.
    assert follower.result.request["message_id"] == record["message_id"]
    assert validate(follower.result.request) == (True, [])
    assert validate(follower.result.response) == (True, [])
    assert follower.result.response["payload"]["response"] == "COMPLY"
    assert (
        follower.result.response["payload"]["in_reply_to"]
        == follower.result.request["message_id"]
    )
    assert follower.result.response["recipient_id"] == COMMANDER_ID
    # HOLD stops the follower.
    assert follower.state.movement_flag is False
    assert follower.state.current_command is None


def test_report_status_returns_follower_state():
    follower = FollowerProcess("follower-1")
    code, (record,), _ = run_cli(
        "--follower", follower.spec, "--command", "REPORT_STATUS"
    )
    follower.join()

    assert code == EXIT_OK
    assert record["outcome"] == "STATUS_RESPONSE"
    assert record["state"]["agent_id"] == "follower-1"
    assert record["state"]["battery_percent"] == 100


def test_proceed_to_waypoint_carries_waypoint():
    follower = FollowerProcess("follower-1")
    code, (record,), _ = run_cli(
        "--follower",
        follower.spec,
        "--command",
        "PROCEED_TO_WAYPOINT",
        "--waypoint",
        "WP-7",
    )
    follower.join()

    assert code == EXIT_OK
    assert record["outcome"] == "COMPLY"
    assert follower.result.request["payload"]["waypoint"] == "WP-7"


def test_decline_at_low_battery_preserves_state():
    state = FollowerState(parent_id=COMMANDER_ID)
    state.force_state({"battery_percent": 5})
    before = copy.deepcopy(state)
    follower = FollowerProcess("follower-1", state)

    code, (record,), summary = run_cli(
        "--follower", follower.spec, "--command", "RETURN"
    )
    follower.join()

    # A decline is still a valid answer, so the round trip itself succeeded.
    assert code == EXIT_OK
    assert record["command"] == "RETURN"
    assert record["outcome"] == "DECLINE"
    assert record["reason"] == "BATTERY_CRITICAL"
    assert summary["answered"] == 1
    assert validate(follower.result.request) == (True, [])
    assert validate(follower.result.response) == (True, [])
    assert (
        follower.result.response["payload"]["in_reply_to"]
        == follower.result.request["message_id"]
    )
    assert follower.result.lifecycle == (
        CommandLifecycle.READY,
        CommandLifecycle.COMMAND_RECEIVED,
        CommandLifecycle.EVALUATING,
        CommandLifecycle.DECLINED,
    )
    assert follower.state == before


def test_low_battery_hold_and_status_follow_non_movement_rules():
    hold_state = FollowerState(parent_id=COMMANDER_ID, battery_percent=5)
    hold_follower = FollowerProcess("follower-1", hold_state)
    code, (record,), _ = run_cli("--follower", hold_follower.spec)
    hold_follower.join()

    assert code == EXIT_OK
    assert record["outcome"] == "COMPLY"
    assert hold_follower.state == hold_state

    status_state = FollowerState(parent_id=COMMANDER_ID, battery_percent=5)
    status_before = copy.deepcopy(status_state)
    status_follower = FollowerProcess("follower-2", status_state)
    code, (record,), _ = run_cli(
        "--follower", status_follower.spec, "--command", "REPORT_STATUS"
    )
    status_follower.join()

    assert code == EXIT_OK
    assert record["outcome"] == "STATUS_RESPONSE"
    assert record["state"]["battery_percent"] == 5
    assert status_follower.state == status_before


# --- broadcast ------------------------------------------------------------------


def test_broadcast_reaches_three_followers():
    followers = [FollowerProcess(f"follower-{n}", moving()) for n in (1, 2, 3)]
    for follower in followers:
        follower.wait_ready()
    args = []
    for follower in followers:
        args += ["--follower", follower.spec]

    code, records, summary = run_cli(*args)
    for follower in followers:
        follower.join()

    assert code == EXIT_OK
    assert [r["follower"] for r in records] == [
        "follower-1",
        "follower-2",
        "follower-3",
    ]
    assert {r["outcome"] for r in records} == {"COMPLY"}
    assert len({r["message_id"] for r in records}) == 3
    assert summary == {"command": "HOLD", "followers": 3, "answered": 3}
    assert len({follower.agent_id for follower in followers}) == 3
    for follower in followers:
        assert follower.result.request["recipient_id"] == follower.agent_id
        assert validate(follower.result.request) == (True, [])
        assert validate(follower.result.response) == (True, [])
        assert follower.result.response["payload"]["response"] == "COMPLY"
        assert (
            follower.result.response["payload"]["in_reply_to"]
            == follower.result.request["message_id"]
        )
        assert follower.result.response["recipient_id"] == COMMANDER_ID
        assert follower.state.movement_flag is False


def test_broadcast_with_one_low_battery_follower():
    low = FollowerState(parent_id=COMMANDER_ID)
    low.force_state({"battery_percent": 5})
    low_before = copy.deepcopy(low)
    followers = [
        FollowerProcess("follower-1"),
        FollowerProcess("follower-2", low),
        FollowerProcess("follower-3"),
    ]
    for follower in followers:
        follower.wait_ready()
    args = []
    for follower in followers:
        args += ["--follower", follower.spec]

    code, records, _ = run_cli(*args, "--command", "RETURN")
    for follower in followers:
        follower.join()

    assert code == EXIT_OK
    assert [r["outcome"] for r in records] == ["COMPLY", "DECLINE", "COMPLY"]
    assert records[1]["reason"] == "BATTERY_CRITICAL"
    low_result = followers[1].result
    assert validate(low_result.request) == (True, [])
    assert validate(low_result.response) == (True, [])
    assert (
        low_result.response["payload"]["in_reply_to"]
        == low_result.request["message_id"]
    )
    assert low_result.lifecycle[-1] is CommandLifecycle.DECLINED
    assert low == low_before


# --- failures -------------------------------------------------------------------


def test_silent_follower_times_out():
    peer = SilentPeer()
    timeout = min(ACCEPTANCE_TIMEOUT_SECONDS, 0.3)
    try:
        start = time.monotonic()
        code, (record,), summary = run_cli(
            "--follower",
            f"quiet=127.0.0.1:{peer.port}",
            "--timeout",
            str(timeout),
        )
        elapsed = time.monotonic() - start
    finally:
        peer.stop()

    assert code == EXIT_UNANSWERED
    assert record["outcome"] == "TIMEOUT"
    assert summary["answered"] == 0
    assert elapsed < 3


def test_one_timeout_does_not_hide_other_answers():
    peer = SilentPeer()
    follower = FollowerProcess("follower-1")
    follower.wait_ready()
    try:
        code, records, summary = run_cli(
            "--follower",
            follower.spec,
            "--follower",
            f"quiet=127.0.0.1:{peer.port}",
            "--timeout",
            "0.5",
        )
    finally:
        peer.stop()
    follower.join()

    assert code == EXIT_UNANSWERED
    assert [r["outcome"] for r in records] == ["COMPLY", "TIMEOUT"]
    assert summary["answered"] == 1


def test_unreachable_follower_is_reported_and_others_still_run():
    follower = FollowerProcess("follower-1")
    code, records, _ = run_cli(
        "--follower",
        follower.spec,
        "--follower",
        f"gone=127.0.0.1:{free_port()}",
        "--connect-retries",
        "0",
        "--connect-timeout",
        "0.5",
    )
    follower.join()

    assert code == EXIT_UNANSWERED
    assert [r["outcome"] for r in records] == ["COMPLY", "UNREACHABLE"]


def test_waits_for_a_follower_that_starts_late():
    follower = FollowerProcess("follower-1", port=free_port(), start_delay=0.8)
    code, (record,), _ = run_cli(
        "--follower", follower.spec, "--connect-retries", "5", "--connect-timeout", "1"
    )
    follower.join()

    assert code == EXIT_OK
    assert record["outcome"] == "COMPLY"


def test_unresolvable_host_is_unreachable():
    code, (record,), _ = run_cli(
        "--follower", "f1=no-such-host.invalid:5000", "--connect-retries", "0"
    )
    assert code == EXIT_UNANSWERED
    assert record["outcome"] == "UNREACHABLE"


def test_host_names_are_resolved():
    follower = FollowerProcess("follower-1")
    code, (record,), _ = run_cli("--follower", f"follower-1=localhost:{follower.port}")
    follower.join()
    assert code == EXIT_OK
    assert record["outcome"] == "COMPLY"


# --- arguments ------------------------------------------------------------------


def test_followers_from_environment(monkeypatch):
    follower = FollowerProcess("follower-1")
    monkeypatch.setenv("FALCON_FOLLOWERS", follower.spec)
    code, (record,), _ = run_cli()
    follower.join()
    assert code == EXIT_OK
    assert record["outcome"] == "COMPLY"


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--follower", "f1=127.0.0.1:5000", "--follower", "f1=127.0.0.1:5001"],
        ["--follower", "f1=127.0.0.1:5000", "--command", "PROCEED_TO_WAYPOINT"],
        ["--follower", "f1=127.0.0.1:5000", "--waypoint", "WP-1"],
        ["--follower", "bad id=127.0.0.1:5000"],
    ],
    ids=["none", "duplicate", "missing-waypoint", "stray-waypoint", "bad-id"],
)
def test_bad_arguments_exit_with_usage_error(args, monkeypatch, capsys):
    monkeypatch.delenv("FALCON_FOLLOWERS", raising=False)
    out = io.StringIO()
    assert main(args, out=out) == EXIT_USAGE
    assert out.getvalue() == ""  # nothing was sent or reported
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["--command", "FLY"],
        ["--timeout", "0"],
        ["--timeout", "-1"],
        ["--connect-retries", "-1"],
        ["--role-epoch", "-1"],
    ],
)
def test_invalid_option_values_are_rejected(args):
    with pytest.raises(SystemExit) as exit_info:
        main(["--follower", "f1=127.0.0.1:5000", *args], out=io.StringIO())
    assert exit_info.value.code == EXIT_USAGE


@pytest.mark.parametrize(
    "spec",
    ["f1", "f1=127.0.0.1", "f1=:5000", "=127.0.0.1:5000", "f1=h:0", "f1=h:70000"],
)
def test_parse_follower_rejects_malformed_specs(spec):
    with pytest.raises(ValueError):
        parse_follower(spec)


def test_parse_follower_accepts_host_names():
    address = parse_follower("follower-2=follower-2:5000")
    assert (address.follower_id, address.host, address.port) == (
        "follower-2",
        "follower-2",
        5000,
    )


# --- separate process -----------------------------------------------------------


def test_runs_as_a_separate_process():
    follower = FollowerProcess("follower-1")
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    env.pop("FALCON_FOLLOWERS", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.agents.commander_runner",
            "--follower",
            follower.spec,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    follower.join()

    assert completed.returncode == EXIT_OK, completed.stderr
    record = json.loads(completed.stdout.splitlines()[0])
    assert record["outcome"] == "COMPLY"


# --- latency --------------------------------------------------------------------


def test_latency_is_measured_with_a_precise_clock(monkeypatch):
    # The runner must time replies with perf_counter. On Windows, monotonic()
    # only ticks every ~16 ms, so fast replies used to show as 0 ms.
    ticks = iter([100.0, 100.0042])
    monkeypatch.setattr(commander_runner, "perf_counter", lambda: next(ticks))
    follower = FollowerProcess("follower-1")
    follower.wait_ready()

    code, (record,), _ = run_cli("--follower", follower.spec)
    follower.join()

    assert code == EXIT_OK
    assert record["latency_ms"] == 4.2


def test_real_round_trip_reports_nonzero_latency():
    follower = FollowerProcess("follower-1")
    follower.wait_ready()

    code, (record,), _ = run_cli("--follower", follower.spec)
    follower.join()

    assert code == EXIT_OK
    assert record["latency_ms"] > 0
