"""Real agent entry points, stateful exchanges, and process restart registration.

The injected policy is deliberately the small demo policy, not Phase-4 policy.
All subprocess output is drained in threads and observed with finite queue waits.
"""

import json
import os
import signal
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from threading import Thread

import pytest

from src.schema.validator import validate

ROOT = Path(__file__).resolve().parents[2]


class Process:
    def __init__(self, *args):
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"FALCON_AGENT_ID", "FALCON_FOLLOWERS"}
        }
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-m", *args],
            cwd=ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        self.out, self.err = Queue(), Queue()
        self.errors = []
        self.readers = []
        for stream, queue, errors in (
            (self.process.stdout, self.out, None),
            (self.process.stderr, self.err, self.errors),
        ):

            def drain(stream=stream, queue=queue, errors=errors):
                for line in stream:
                    if errors is not None:
                        errors.append(line)
                    queue.put(line)

            reader = Thread(target=drain, daemon=True)
            reader.start()
            self.readers.append(reader)

    def record(self):
        try:
            return json.loads(self.out.get(timeout=5))
        except Empty:
            pytest.fail(f"No output: exit={self.process.poll()}, stderr={self.errors}")

    def send(self, command):
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()

    def wait_for_retry(self):
        for _ in range(20):
            try:
                line = self.err.get(timeout=3)
            except Empty:
                pytest.fail(f"No retry log: {self.errors}")
            if "retrying in" in line:
                return line
        pytest.fail(f"No retry log: {self.errors}")

    def stop(self, *, crash=False):
        if self.process.poll() is None:
            if crash:
                self.process.kill()
            else:
                self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        for reader in self.readers:
            reader.join(timeout=1)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


@pytest.fixture
def processes():
    running = []

    def start(*args):
        child = Process(*args)
        running.append(child)
        return child

    yield start
    for child in reversed(running):
        child.stop()


def start_follower(start, *, port=0, state=None, agent_id="follower-1"):
    args = [
        "src.agents.follower_runner",
        "--port",
        str(port),
        "--agent-id",
        agent_id,
        "--decision-engine",
        "scripts.tcp_agent_policy:decide",
    ]
    if state is not None:
        args += ["--state", str(state)]
    follower = start(*args)
    ready = follower.record()
    assert ready["event"] == "listening"
    return follower, ready["port"]


def start_commander(start, port, *, retries=10):
    commander = start(
        "src.agents.commander_runner",
        "--interactive",
        "--follower",
        f"follower-1=127.0.0.1:{port}",
        "--timeout",
        "0.5",
        "--connect-timeout",
        "0.1",
        "--connect-retries",
        str(retries),
        "--initial-backoff",
        "0.02",
        "--max-backoff",
        "0.04",
    )
    registration = commander.record()
    assert registration["event"] == "registered", registration
    assert registration["state"]["agent_id"] == "follower-1"
    return commander


def assert_exchange(record, command):
    assert record["event"] == "command"
    assert record["request"]["payload"]["command"] == command
    assert validate(record["request"])[0]
    assert validate(record["response"])[0]
    assert (
        record["response"]["payload"]["in_reply_to"] == record["request"]["message_id"]
    )
    assert "hierarchy_level" not in record["state"]


def test_real_agent_processes_all_commands_and_state(processes, tmp_path):
    scenario = tmp_path / "moving.yml"
    scenario.write_text("movement_flag: true\ncurrent_command: RETURN\n")
    follower, port = start_follower(processes, state=scenario)
    commander = start_commander(processes, port)
    registration = follower.record()
    assert registration["event"] == "registered"
    assert registration["state"]["movement_flag"] is True  # probe is read-only
    for text, expected, position, moving in [
        ("HOLD", "COMPLY", "base", False),
        ("PROCEED_TO_WAYPOINT north-fence", "COMPLY", "north-fence", True),
        ("REPORT_STATUS", "STATUS_RESPONSE", "north-fence", True),
        ("RETURN", "COMPLY", "base", True),
        ("REPORT_STATUS", "STATUS_RESPONSE", "base", True),
    ]:
        commander.send(text)
        reply = commander.record()
        assert reply["outcome"] == expected
        exchange = follower.record()
        assert_exchange(exchange, text.split()[0])
        assert exchange["state"]["position"] == position
        assert exchange["state"]["movement_flag"] is moving
        if expected == "STATUS_RESPONSE":
            assert reply["state"] == exchange["state"]
    commander.send("QUIT")
    assert commander.process.wait(timeout=2) == 0


def test_low_battery_decline_uses_real_follower(processes, tmp_path):
    scenario = tmp_path / "low.yml"
    scenario.write_text("battery_percent: 5\nposition: north-fence\n")
    follower, port = start_follower(processes, state=scenario)
    commander = start_commander(processes, port)
    before = follower.record()["state"]
    commander.send("RETURN")
    reply = commander.record()
    assert reply["outcome"] == "DECLINE"
    assert reply["reason"] == "BATTERY_CRITICAL"
    exchange = follower.record()
    assert_exchange(exchange, "RETURN")
    assert exchange["state"] == before
    commander.send("HOLD")
    assert commander.record()["outcome"] == "COMPLY"
    assert_exchange(follower.record(), "HOLD")


def test_crashed_follower_restarts_registers_and_commander_recovers(processes):
    follower, port = start_follower(processes)
    commander = start_commander(processes, port)
    follower.record()  # initial registration
    commander.send("HOLD")
    assert commander.record()["outcome"] == "COMPLY"
    follower.record()
    follower.stop(crash=True)
    commander.send("PROCEED_TO_WAYPOINT north-fence")
    assert commander.record()["event"] == "connection_lost"
    assert "retrying in" in commander.wait_for_retry()
    replacement, _ = start_follower(processes, port=port)
    assert commander.record()["event"] == "registered"
    unknown = commander.record()
    assert unknown["outcome"] == "DELIVERY_UNKNOWN"
    assert unknown["recovered"] is True
    assert unknown["unreachable"] is False
    assert replacement.record()["event"] == "registered"
    # The interrupted movement command was never replayed to the new process.
    commander.send("REPORT_STATUS")
    report = commander.record()
    assert report["outcome"] == "STATUS_RESPONSE"
    assert report["state"]["position"] == "base"
    assert report["state"]["current_command"] is None
    assert_exchange(replacement.record(), "REPORT_STATUS")
    commander.send("HOLD")
    assert commander.record()["outcome"] == "COMPLY"
    assert_exchange(replacement.record(), "HOLD")


def test_retry_exhaustion_marks_runtime_unreachable(processes):
    follower, port = start_follower(processes)
    commander = start_commander(processes, port, retries=1)
    follower.record()
    follower.stop(crash=True)
    commander.send("HOLD")
    assert commander.record()["event"] == "connection_lost"
    failure = commander.record()
    assert failure["event"] == "recovery_failed"
    assert failure["unreachable"] is True
    unknown = commander.record()
    assert unknown["recovered"] is False
    assert unknown["unreachable"] is True
    # Explicit recovery remains possible after the bounded window is exhausted.
    replacement, _ = start_follower(processes, port=port)
    commander.send("RECONNECT")
    assert commander.record()["event"] == "registered"
    assert replacement.record()["event"] == "registered"
    commander.send("HOLD")
    assert commander.record()["outcome"] == "COMPLY"
    assert_exchange(replacement.record(), "HOLD")


def test_explicit_reconnect_preserves_live_follower_state(processes):
    follower, port = start_follower(processes)
    commander = start_commander(processes, port)
    follower.record()
    commander.send("PROCEED_TO_WAYPOINT gate")
    assert commander.record()["outcome"] == "COMPLY"
    follower.record()
    commander.send("RECONNECT")
    assert commander.record()["state"]["position"] == "gate"
    assert follower.record()["event"] == "registered"
    commander.send("REPORT_STATUS")
    assert commander.record()["state"]["position"] == "gate"
    assert_exchange(follower.record(), "REPORT_STATUS")


def test_clean_follower_shutdown_while_idle(processes):
    follower, _ = start_follower(processes)
    # TerminateProcess (Popen.terminate on Windows) cannot run Python cleanup.
    # CTRL_BREAK_EVENT reaches the runner's graceful signal handler instead.
    if sys.platform == "win32":
        follower.process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        follower.process.send_signal(signal.SIGTERM)
    assert follower.process.wait(timeout=5) == 0
