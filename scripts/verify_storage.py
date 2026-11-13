"""Check that every exchange in the agents' JSONL logs is in the database.

Reads every exchange log under a directory, asks the storage service to ingest,
then compares each logged message record with what the service's API returns
for that session: same message, same agent, same direction, same
hierarchy_level. Uses only the standard library.

    python -m scripts.verify_storage --logs logs/mvp/<session> \\
        --api http://localhost:8080

Inside the Compose network the API is http://storage-api:8080 and the logs are
mounted at /logs. Exit status is 0 when every record is stored, 1 when any is
missing or differs, and 2 when the logs or the API can't be read.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

PAGE = 1000


def logged_messages(root: Path) -> Counter:
    """(session, message_id, logged_by, DIRECTION, level) for every message record."""
    records: Counter = Counter()
    for path in sorted(root.rglob("*.jsonl*")):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("record_type") != "message":
                continue
            records[
                (
                    record["session_id"],
                    record["message"]["message_id"],
                    record["logged_by"],
                    record["direction"].upper(),
                    record.get("hierarchy_level"),
                )
            ] += 1
    return records


def _request(api: str, path: str, *, method: str = "GET", timeout: float = 30.0):
    request = urllib.request.Request(api.rstrip("/") + path, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def stored_messages(api: str, session: str) -> Counter:
    rows: Counter = Counter()
    offset = 0
    while True:
        query = urllib.parse.urlencode(
            {"sessionId": session, "limit": PAGE, "offset": offset}
        )
        page = _request(api, f"/api/messages?{query}")
        for item in page["items"]:
            rows[
                (
                    item["sessionId"],
                    item["messageId"],
                    item["loggedBy"],
                    item["direction"],
                    item["hierarchyLevel"],
                )
            ] += 1
        offset += len(page["items"])
        if not page["items"] or offset >= page["total"]:
            return rows


def verify(logs: Path, api: str, *, ingest: bool = True) -> dict:
    expected = logged_messages(logs)
    if not expected:
        raise ValueError(f"no message records found under {logs}")
    ingested = _request(api, "/api/ingest", method="POST") if ingest else None

    sessions = sorted({key[0] for key in expected})
    stored: Counter = Counter()
    for session in sessions:
        stored += stored_messages(api, session)

    missing = expected - stored
    unexpected = stored - expected
    return {
        "ok": not missing and not unexpected,
        "sessions": sessions,
        "logged_records": sum(expected.values()),
        "stored_rows": sum(stored.values()),
        "missing": [list(key) for key in sorted(missing, key=str)],
        "unexpected": [list(key) for key in sorted(unexpected, key=str)],
        "ingest": ingested,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.verify_storage", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--api", default="http://localhost:8080")
    parser.add_argument(
        "--no-ingest", action="store_true", help="compare without ingesting first"
    )
    args = parser.parse_args(argv)
    try:
        report = verify(args.logs, args.api, ingest=not args.no_ingest)
    except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
        print(f"verify_storage: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
