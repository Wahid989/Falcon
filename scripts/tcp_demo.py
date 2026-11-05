"""Scripted transport-only HOLD/COMPLY demo; not an agent decision engine.

Run from the repository root with python -m scripts.tcp_demo --help.
"""

import argparse
import json
from datetime import datetime, timezone
from uuid import uuid4

from src.transport import TCPClientTransport, TCPServerTransport, TransportError


def envelope(message_type, payload, *, sender_role):
    """Build a v1 demo envelope; callers still own schema validation."""
    commander = sender_role == "commander"
    return {
        "message_id": str(uuid4()),
        "sender_id": "commander" if commander else "follower",
        "recipient_id": "follower" if commander else "commander",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "schema_version": 1,
        "sender_role": sender_role,
        "role_epoch": 0,
        "type": message_type,
        "payload": payload,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("follower", "commander"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    link = (
        TCPServerTransport(args.host, args.port, connect_timeout=args.timeout)
        if args.role == "follower"
        else TCPClientTransport(args.host, args.port, connect_timeout=args.timeout)
    )
    try:
        if args.role == "follower":
            host, port = link.listen()
            print(json.dumps({"listening": {"host": host, "port": port}}), flush=True)
        link.connect()
        if args.role == "commander":
            link.send(envelope("command", {"command": "HOLD"}, sender_role="commander"))
        incoming = link.receive(args.timeout)
        print(json.dumps({"received": incoming}), flush=True)
        if args.role == "follower":
            # Deliberately scripted, with no simulated state or authority policy.
            link.send(
                envelope(
                    "response",
                    {"response": "COMPLY", "in_reply_to": incoming["message_id"]},
                    sender_role="follower",
                )
            )
    except (TransportError, ValueError, KeyError) as exc:
        parser.exit(1, f"Transport demo failed: {exc}\n")
    finally:
        link.close()


if __name__ == "__main__":
    main()
