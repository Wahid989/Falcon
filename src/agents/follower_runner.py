"""Persistent TCP Follower entry point with an explicitly injected decision policy."""

import argparse
import importlib
import json
import logging
import os
import signal
import sys

from src.agents.tcp_runtime import FollowerTCPServer
from src.logging.exchange import ExchangeLogger, ExchangeLogError
from src.state import FollowerState, load_scenario
from src.transport import TransportError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", default="follower-1")
    parser.add_argument("--commander-id", default="commander")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--log-dir", default=os.environ.get("FALCON_LOG_DIR"))
    parser.add_argument(
        "--session-id", default=os.environ.get("FALCON_SESSION_ID", "manual")
    )
    parser.add_argument("--state", help="YAML state overrides, loaded once at startup")
    parser.add_argument(
        "--decision-engine",
        default="src.decision.engine:decide",
        metavar="MODULE:CALLABLE",
    )
    parser.add_argument("--max-commands", type=int)
    parser.add_argument(
        "--pretty",
        action="store_true",
        default=os.environ.get("FALCON_PRETTY_OUTPUT") == "1",
        help="print indented JSON for terminal presentations",
    )
    args = parser.parse_args(argv)

    def report(record):
        text = json.dumps(record, indent=2 if args.pretty else None)
        print(f"\n{text}\n" if args.pretty else text, flush=True)

    server = None
    previous_handlers = {}
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        module, separator, name = args.decision_engine.partition(":")
        if not separator or not module or not name:
            raise ValueError("Decision engine must be MODULE:CALLABLE")
        policy = getattr(importlib.import_module(module), name)
        state = load_scenario(args.state) if args.state else FollowerState()
        exchange_log = (
            ExchangeLogger(
                args.log_dir,
                args.agent_id,
                session_id=args.session_id,
                level=1,
                peer_levels={args.commander_id: 0},
            )
            if args.log_dir
            else None
        )
        server = FollowerTCPServer(
            args.agent_id,
            args.commander_id,
            policy,
            host=args.host,
            port=args.port,
            state=state,
            exchange_log=exchange_log,
            report=report,
        )
        host, port = server.listen()

        def stop(*_):
            raise KeyboardInterrupt

        for number in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[number] = signal.signal(number, stop)
        report({"event": "listening", "host": host, "port": port})
        server.serve(max_commands=args.max_commands)
        return 0
    except KeyboardInterrupt:
        return 0
    except (
        ValueError,
        TypeError,
        ImportError,
        AttributeError,
        OSError,
        TransportError,
        ExchangeLogError,
    ) as exc:
        print(f"Follower failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.close()
        for number, handler in previous_handlers.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    sys.exit(main())
