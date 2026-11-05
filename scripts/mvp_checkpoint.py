"""Verify live agents and JSONL evidence; print GO only after every check passes."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time

from src.agents import Commander, Outcome
from src.agents.commander_runner import open_link, parse_follower
from src.logging.exchange import ExchangeLogger
from src.schema.validator import validate

FOLLOWER_IDS = ("follower-1", "follower-2", "follower-3", "follower-low")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify_logs(directory, session_id, exchanges):
    """Each request/response must appear once at both sending and receiving agents."""
    expected = {}
    for result in exchanges:
        for message in (result.command, result.response):
            require(message is not None, "Missing response")
            expected[message["message_id"]] = message
    receipts = Counter()
    for path in Path(directory).glob("*.jsonl"):
        data = path.read_text(encoding="utf-8")
        require(not data or data.endswith("\n"), f"Incomplete JSONL line: {path.name}")
        for line in data.splitlines():
            record = json.loads(line)
            if record.get("session_id") != session_id:
                continue
            message = record.get("message", {})
            message_id = message.get("message_id")
            if message_id not in expected:
                continue
            require(record.get("record_type") == "message", "Invalid log record type")
            require(
                message == expected[message_id],
                "Logged envelope differs from wire message",
            )
            require(validate(message)[0], "Invalid schema in exchange log")
            direction = record.get("direction")
            require(direction in {"sent", "received"}, "Invalid log direction")
            logger = (
                message["sender_id"] if direction == "sent" else message["recipient_id"]
            )
            require(record.get("logged_by") == logger, "Incorrect logging agent")
            require(path.name == f"{logger}.jsonl", "Exchange in incorrect agent file")
            expected_level = 0 if message["sender_id"] == "commander" else 1
            require(
                record.get("hierarchy_level") == expected_level,
                "Incorrect log hierarchy level",
            )
            require(bool(record.get("logged_at")), "Missing log timestamp")
            receipts[(message_id, direction)] += 1
    for message_id in expected:
        require(receipts[(message_id, "sent")] == 1, "Missing/duplicate sender log")
        require(
            receipts[(message_id, "received")] == 1, "Missing/duplicate receiver log"
        )
    return {
        "wire_messages": len(expected),
        "message_log_records": sum(receipts.values()),
    }


def run_checkpoint(followers, *, log_dir, session_id, timeout=5):
    require(
        set(followers) == set(FOLLOWER_IDS),
        "Exactly four named MVP followers are required",
    )
    logger = ExchangeLogger(
        log_dir,
        "commander",
        session_id=session_id,
        level=0,
        peer_levels={name: 1 for name in followers},
    )
    commander = Commander("commander", exchange_log=logger)
    links, exchanges, latencies = [], [], []

    def collect(command, deadline):
        result = commander.await_response(command, max(0, deadline - time.monotonic()))
        require(
            result.response is not None, f"No response from {command['recipient_id']}"
        )
        require(
            validate(result.command)[0] and validate(result.response)[0],
            "Invalid envelope",
        )
        require(
            result.response["payload"]["in_reply_to"] == command["message_id"],
            "Correlation mismatch",
        )
        exchanges.append(result)
        return result

    def issue(follower, command):
        start = time.monotonic()
        request = commander.send_command(follower, command)
        result = collect(request, start + timeout)
        elapsed = time.monotonic() - start
        require(elapsed < timeout, "Round trip exceeded timeout")
        latencies.append(elapsed * 1000)
        return result

    def status(follower):
        result = issue(follower, "REPORT_STATUS")
        require(result.outcome is Outcome.STATUS_RESPONSE, "Status request failed")
        require(result.state.get("agent_id") == follower, "Incorrect state identity")
        return result.state

    try:
        for name, address in followers.items():
            link = open_link(address, connect_timeout=2, retries=5)
            links.append(link)
            commander.add_follower(name, link)
        before = {name: status(name) for name in followers}
        require(
            before["follower-low"]["battery_percent"] == 5,
            "Decline battery must be exactly 5%",
        )
        require(
            before["follower-1"]["movement_flag"] is True,
            "Single follower must start moving",
        )
        single = issue("follower-1", "HOLD")
        require(single.outcome is Outcome.COMPLY, "HOLD did not comply")
        single_hold_ms = latencies[-1]
        stopped = status("follower-1")
        require(
            stopped["movement_flag"] is False and stopped["current_command"] is None,
            "HOLD did not clear movement/current command",
        )
        # Broadcast fans out through the actual Commander, then uses one deadline.
        commander.remove_follower("follower-low")
        start = time.monotonic()
        broadcast = commander.broadcast("HOLD")
        require(
            not broadcast.failed and len(broadcast.sent) == 3, "Broadcast send failed"
        )
        for request in broadcast.sent.values():
            require(
                collect(request, start + timeout).outcome is Outcome.COMPLY,
                "Broadcast follower did not comply",
            )
        broadcast_ms = (time.monotonic() - start) * 1000
        require(broadcast_ms < timeout * 1000, "Broadcast exceeded timeout")
        commander.add_follower(
            "follower-low", links[list(followers).index("follower-low")]
        )
        after = {name: status(name) for name in FOLLOWER_IDS[:3]}
        for name in FOLLOWER_IDS[:3]:
            expected = dict(before[name], movement_flag=False, current_command=None)
            require(
                after[name] == expected, "Broadcast changed unexpected state fields"
            )
        decline = issue("follower-low", "RETURN")
        require(
            decline.outcome is Outcome.DECLINE and decline.reason == "BATTERY_CRITICAL",
            "Low-battery movement did not decline with BATTERY_CRITICAL",
        )
        low_after = status("follower-low")
        require(low_after == before["follower-low"], "Decline changed follower state")
        # A response can arrive just before its sender finishes its append.
        deadline = time.monotonic() + 2
        while True:
            try:
                evidence = verify_logs(log_dir, session_id, exchanges)
                break
            except ValueError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        return {
            "decision": "GO",
            "session_id": session_id,
            "checks": {
                "single_hold": True,
                "broadcast_three": True,
                "battery_5_decline": True,
                "state_preserved": True,
                "schema_and_correlation": True,
                "both_sides_logged": True,
            },
            "timeout_seconds": timeout,
            "max_round_trip_ms": round(max(latencies), 3),
            "broadcast_ms": round(broadcast_ms, 3),
            "single_hold_ms": round(single_hold_ms, 3),
            "broadcast_followers": list(FOLLOWER_IDS[:3]),
            "evidence": evidence,
            "low_battery_before": before["follower-low"],
            "low_battery_after": low_after,
            "policy": "limited injected demo policy; full Phase-4 policy is separate",
        }
    finally:
        for link in links:
            link.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--follower", action="append", required=True)
    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    try:
        require(0 < args.timeout <= 60, "timeout must be between 0 and 60 seconds")
        addresses = [parse_follower(spec) for spec in args.follower]
        require(
            len({a.follower_id for a in addresses}) == len(addresses),
            "Duplicate follower ID",
        )
        report = run_checkpoint(
            {a.follower_id: a for a in addresses},
            log_dir=args.log_dir,
            session_id=args.session_id,
            timeout=args.timeout,
        )
    except Exception as exc:
        report = {"decision": "NO_GO", "session_id": args.session_id, "error": str(exc)}
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("\n" + json.dumps(report, indent=2, sort_keys=True) + "\n", flush=True)
    return 0 if report["decision"] == "GO" else 1


if __name__ == "__main__":
    sys.exit(main())
