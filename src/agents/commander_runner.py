"""Command-line runner for the Commander agent.

Connects to one or more followers over TCP, sends one command (to every
follower when there are several), waits for the responses within a single
deadline, and prints one JSON line per follower.

    python -m src.agents.commander --follower follower-1=127.0.0.1:5000
    python -m src.agents.commander --follower f1=10.0.0.5:5000 \\
        --follower f2=10.0.0.6:5000 --follower f3=10.0.0.7:5000

Followers may also be given as ``FALCON_FOLLOWERS="f1=host:port,f2=host:port"``.

Exit status: 0 when every follower answered with a valid response, 1 when any
follower timed out, sent an invalid response, or could not be reached, and 2
for bad arguments.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import time
from time import perf_counter
from dataclasses import dataclass
from re import fullmatch
from typing import Callable, Sequence, TextIO

from src.agents.commander import (
    COMMANDS,
    Commander,
    CommandResult,
    InvalidCommandError,
    Outcome,
    Validator,
)
from src.logging.exchange import ExchangeLogger, ExchangeLogError
from src.transport import (
    ReconnectingTransport,
    TCPClientTransport,
    Transport,
    TransportError,
)

EXIT_OK = 0
EXIT_UNANSWERED = 1
EXIT_USAGE = 2

# Outcomes where the follower sent back a valid response.
ANSWERED = {
    outcome.value
    for outcome in (
        Outcome.COMPLY,
        Outcome.DECLINE,
        Outcome.STATUS_RESPONSE,
        Outcome.ERROR,
    )
}


@dataclass(frozen=True)
class FollowerAddress:
    follower_id: str
    host: str
    port: int


def parse_follower(spec: str) -> FollowerAddress:
    """Parse ``ID=HOST:PORT``."""
    follower_id, sep, address = spec.strip().partition("=")
    host, colon, port_text = address.rpartition(":")
    if not sep or not colon or not host:
        raise ValueError(f"expected ID=HOST:PORT, got {spec!r}")
    if fullmatch(r"[A-Za-z0-9_-]+", follower_id) is None:
        raise ValueError(f"invalid follower id {follower_id!r}")
    if not port_text.isdigit() or not 0 < int(port_text) <= 65535:
        raise ValueError(f"invalid port in {spec!r}")
    return FollowerAddress(follower_id, host, int(port_text))


def _positive(text: str) -> float:
    value = float(text)
    if not value > 0 or value == float("inf"):
        raise argparse.ArgumentTypeError("must be a positive number of seconds")
    return value


def _non_negative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be zero or more")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.agents.commander",
        description="Send a FALCON command to one or more followers over TCP.",
    )
    parser.add_argument(
        "--agent-id",
        default=os.environ.get("FALCON_AGENT_ID", "commander"),
        help="this commander's agent id (env FALCON_AGENT_ID)",
    )
    parser.add_argument(
        "--follower",
        action="append",
        default=[],
        metavar="ID=HOST:PORT",
        help="a follower to command; repeat for several (env FALCON_FOLLOWERS)",
    )
    parser.add_argument("--log-dir", default=os.environ.get("FALCON_LOG_DIR"))
    parser.add_argument(
        "--session-id", default=os.environ.get("FALCON_SESSION_ID", "manual")
    )
    parser.add_argument("--command", default="HOLD", choices=COMMANDS)
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="keep one registered session; read commands from stdin",
    )
    parser.add_argument("--initial-backoff", type=_positive, default=1.0)
    parser.add_argument("--max-backoff", type=_positive, default=30.0)
    parser.add_argument("--waypoint", help="target for PROCEED_TO_WAYPOINT")
    parser.add_argument("--role-epoch", type=_non_negative_int, default=0)
    parser.add_argument(
        "--timeout",
        type=_positive,
        default=5.0,
        help="seconds to wait for all responses (default 5)",
    )
    parser.add_argument(
        "--connect-timeout",
        type=_positive,
        default=5.0,
        help="seconds per connection attempt (default 5)",
    )
    parser.add_argument(
        "--connect-retries",
        type=_non_negative_int,
        default=5,
        help="extra connection attempts while followers start up (default 5)",
    )
    return parser


def _followers_from(args: argparse.Namespace) -> list[FollowerAddress]:
    specs = list(args.follower)
    if not specs:
        specs = [s for s in os.environ.get("FALCON_FOLLOWERS", "").split(",") if s]
    followers = [parse_follower(spec) for spec in specs]
    ids = [f.follower_id for f in followers]
    if len(set(ids)) != len(ids):
        raise ValueError("follower ids must be unique")
    if not followers:
        raise ValueError("no followers given; use --follower or FALCON_FOLLOWERS")
    return followers


def open_link(
    address: FollowerAddress, *, connect_timeout: float, retries: int
) -> Transport:
    """Connect to a follower, retrying with backoff while it starts up.

    The TCP transport only accepts IPv4 literals, so a host name such as a
    Compose service name is resolved here first. A container that is not up
    yet may not resolve either, so resolution shares the same retry budget.
    """
    backoff = 1.0
    attempt = 0
    while True:
        try:
            ip = socket.gethostbyname(address.host)
            break
        except OSError as exc:
            if attempt >= retries:
                raise TransportError(f"Cannot resolve host {address.host!r}") from exc
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            attempt += 1
    link = ReconnectingTransport(
        lambda: TCPClientTransport(ip, address.port, connect_timeout=connect_timeout),
        max_retries=retries - attempt,
        initial_backoff=backoff,
        max_backoff=30.0,
    )
    link.connect()
    return link


def _report(
    follower_id: str, command: str, outcome: str, **extra: object
) -> dict[str, object]:
    record: dict[str, object] = {
        "follower": follower_id,
        "command": command,
        "outcome": outcome,
    }
    record.update({key: value for key, value in extra.items() if value is not None})
    return record


def _result_record(result: CommandResult, latency: float) -> dict[str, object]:
    return _report(
        result.command["recipient_id"],
        result.command["payload"]["command"],
        result.outcome.value,
        message_id=result.command["message_id"],
        latency_ms=round(latency * 1000, 2),
        reason=result.reason,
        error=result.error,
        state=result.state,
        problems=list(result.problems) or None,
    )


def run(
    args: argparse.Namespace,
    *,
    out: TextIO,
    validator: Validator | None = None,
    connect: Callable[..., Transport] = open_link,
) -> int:
    followers = _followers_from(args)
    if args.interactive:
        return run_interactive(args, followers, out=out)
    exchange_log = _exchange_logger(args, followers)
    commander = Commander(
        args.agent_id,
        role_epoch=args.role_epoch,
        validator=validator,
        exchange_log=exchange_log,
    )
    # Fail on a bad command before opening any connection.
    for follower in followers:
        commander.build_command(
            follower.follower_id, args.command, waypoint=args.waypoint
        )

    records: list[dict[str, object]] = []
    links: list[Transport] = []
    try:
        for follower in followers:
            try:
                link = connect(
                    follower,
                    connect_timeout=args.connect_timeout,
                    retries=args.connect_retries,
                )
            except TransportError as exc:
                records.append(
                    _report(
                        follower.follower_id,
                        args.command,
                        "UNREACHABLE",
                        detail=str(exc),
                    )
                )
                continue
            links.append(link)
            commander.add_follower(follower.follower_id, link)

        sent_at = time.monotonic()
        # Latency uses perf_counter: monotonic() ticks only every ~16 ms on
        # Windows, which made fast replies report as 0 ms.
        started = perf_counter()
        broadcast = commander.broadcast(args.command, waypoint=args.waypoint)
        for follower_id, exc in broadcast.failed.items():
            records.append(
                _report(follower_id, args.command, "SEND_FAILED", detail=str(exc))
            )

        deadline = sent_at + args.timeout
        for message in broadcast.sent.values():
            remaining = max(deadline - time.monotonic(), 0.0)
            try:
                result = commander.await_response(message, remaining)
            except TransportError as exc:
                records.append(
                    _report(
                        message["recipient_id"],
                        args.command,
                        "LINK_FAILED",
                        message_id=message["message_id"],
                        detail=str(exc),
                    )
                )
                continue
            records.append(_result_record(result, perf_counter() - started))
    finally:
        for link in links:
            link.close()

    order = {f.follower_id: i for i, f in enumerate(followers)}
    records.sort(key=lambda record: order[record["follower"]])
    for record in records:
        print(json.dumps(record, sort_keys=True), file=out, flush=True)

    answered = sum(record["outcome"] in ANSWERED for record in records)
    summary = {
        "summary": {
            "command": args.command,
            "followers": len(followers),
            "answered": answered,
        }
    }
    print(json.dumps(summary, sort_keys=True), file=out, flush=True)
    return EXIT_OK if answered == len(followers) else EXIT_UNANSWERED


def _exchange_logger(args, followers):
    if not args.log_dir:
        return None
    return ExchangeLogger(
        args.log_dir,
        args.agent_id,
        session_id=args.session_id,
        level=0,
        peer_levels={f.follower_id: 1 for f in followers},
    )


def run_interactive(args, followers, *, out, source=None):
    """Keep a registered session without replaying interrupted commands."""
    from src.agents.tcp_runtime import CommanderSession, CommandInterruptedError

    if len(followers) != 1:
        raise ValueError("Interactive mode requires exactly one follower")
    address = followers[0]
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    def report(record):
        print(json.dumps(record, sort_keys=True), file=out, flush=True)

    session = CommanderSession(
        args.agent_id,
        address.follower_id,
        address.host,
        address.port,
        role_epoch=args.role_epoch,
        timeout=args.timeout,
        connect_timeout=args.connect_timeout,
        max_retries=args.connect_retries,
        initial_backoff=args.initial_backoff,
        max_backoff=args.max_backoff,
        report=report,
        exchange_log=_exchange_logger(args, followers),
    )
    try:
        session.connect()
        for line in source if source is not None else sys.stdin:
            parts = line.strip().split()
            if not parts:
                continue
            if parts == ["QUIT"]:
                return EXIT_OK
            try:
                if parts == ["RECONNECT"]:
                    session.reconnect()
                    continue
                if len(parts) > 2 or (
                    len(parts) == 2 and parts[0] != "PROCEED_TO_WAYPOINT"
                ):
                    raise ValueError(
                        "Use COMMAND, PROCEED_TO_WAYPOINT NAME, RECONNECT, or QUIT"
                    )
                started = time.monotonic()
                result = session.issue(
                    parts[0], waypoint=parts[1] if len(parts) == 2 else None
                )
                report(_result_record(result, time.monotonic() - started))
            except CommandInterruptedError as exc:
                report(
                    {
                        "outcome": "DELIVERY_UNKNOWN",
                        "message_id": exc.message_id,
                        "recovered": exc.recovered,
                        "unreachable": session.unreachable,
                        "detail": str(exc),
                    }
                )
            except (InvalidCommandError, ValueError) as exc:
                report({"outcome": "INVALID_COMMAND", "detail": str(exc)})
            except TransportError as exc:
                report(
                    {
                        "outcome": (
                            "UNREACHABLE" if session.unreachable else "LINK_FAILED"
                        ),
                        "detail": str(exc),
                    }
                )
        return EXIT_OK
    except TransportError as exc:
        report(
            {
                "outcome": "UNREACHABLE" if session.unreachable else "LINK_FAILED",
                "detail": str(exc),
            }
        )
        return EXIT_UNANSWERED
    except KeyboardInterrupt:
        return EXIT_OK
    finally:
        session.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    out: TextIO | None = None,
    validator: Validator | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args, out=out or sys.stdout, validator=validator)
    except (OSError, ExchangeLogError) as exc:
        print(f"Exchange logging failed: {exc}", file=sys.stderr)
        return EXIT_UNANSWERED
    except (ValueError, InvalidCommandError) as exc:
        parser.print_usage(sys.stderr)
        print(f"{parser.prog}: error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
