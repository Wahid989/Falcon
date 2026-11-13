"""scripts.verify_storage against a fake storage API."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from scripts import verify_storage


def log_line(session, message_id, logged_by, direction, level, record_type="message"):
    return json.dumps(
        {
            "record_type": record_type,
            "session_id": session,
            "logged_by": logged_by,
            "logged_at": "2026-11-13T12:00:00Z",
            "direction": direction,
            "hierarchy_level": level,
            "message": {"message_id": message_id},
        }
    )


def api_row(session, message_id, logged_by, direction, level):
    return {
        "sessionId": session,
        "messageId": message_id,
        "loggedBy": logged_by,
        "direction": direction,
        "hierarchyLevel": level,
    }


@pytest.fixture
def fake_api():
    """Serves /api/messages from `rows` and counts POST /api/ingest calls."""
    state = {"rows": [], "ingests": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            state["ingests"] += 1
            self._reply({"linesRead": 0})

        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            session = query["sessionId"][0]
            limit, offset = int(query["limit"][0]), int(query["offset"][0])
            rows = [r for r in state["rows"] if r["sessionId"] == session]
            self._reply(
                {
                    "items": rows[offset : offset + limit],
                    "total": len(rows),
                    "limit": limit,
                    "offset": offset,
                }
            )

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()
    server.server_close()


def write_logs(tmp_path):
    run = tmp_path / "mvp" / "s1"
    run.mkdir(parents=True)
    (run / "commander.jsonl").write_text(
        log_line("s1", "m-1", "commander", "sent", 0)
        + "\n"
        + log_line("s1", "m-2", "commander", "received", 1)
        + "\n"
        + log_line("s1", "e-1", "commander", "sent", 0, record_type="event")
        + "\n\n"
    )
    (run / "follower-1.jsonl").write_text(
        log_line("s1", "m-1", "follower-1", "received", 0)
        + "\n"
        + log_line("s1", "m-2", "follower-1", "sent", 1)
        + "\n"
    )
    return [
        api_row("s1", "m-1", "commander", "SENT", 0),
        api_row("s1", "m-2", "commander", "RECEIVED", 1),
        api_row("s1", "m-1", "follower-1", "RECEIVED", 0),
        api_row("s1", "m-2", "follower-1", "SENT", 1),
    ]


def test_all_records_stored(tmp_path, fake_api):
    fake_api["rows"] = write_logs(tmp_path)

    report = verify_storage.verify(tmp_path, fake_api["url"])

    assert report["ok"] is True
    assert report["sessions"] == ["s1"]
    assert report["logged_records"] == report["stored_rows"] == 4
    assert fake_api["ingests"] == 1


def test_missing_row_is_reported(tmp_path, fake_api):
    fake_api["rows"] = write_logs(tmp_path)[:-1]

    report = verify_storage.verify(tmp_path, fake_api["url"])

    assert report["ok"] is False
    assert report["missing"] == [["s1", "m-2", "follower-1", "SENT", 1]]
    assert report["unexpected"] == []


def test_wrong_hierarchy_level_is_reported(tmp_path, fake_api):
    rows = write_logs(tmp_path)
    rows[0]["hierarchyLevel"] = 3
    fake_api["rows"] = rows

    report = verify_storage.verify(tmp_path, fake_api["url"])

    assert report["ok"] is False
    assert report["missing"] == [["s1", "m-1", "commander", "SENT", 0]]
    assert report["unexpected"] == [["s1", "m-1", "commander", "SENT", 3]]


def test_pages_through_large_sessions(tmp_path, fake_api, monkeypatch):
    monkeypatch.setattr(verify_storage, "PAGE", 3)
    fake_api["rows"] = write_logs(tmp_path)

    assert verify_storage.verify(tmp_path, fake_api["url"])["ok"] is True


def test_no_ingest_skips_the_post(tmp_path, fake_api):
    fake_api["rows"] = write_logs(tmp_path)

    verify_storage.verify(tmp_path, fake_api["url"], ingest=False)

    assert fake_api["ingests"] == 0


def test_exit_codes(tmp_path, fake_api, capsys):
    fake_api["rows"] = write_logs(tmp_path)
    assert verify_storage.main(["--logs", str(tmp_path), "--api", fake_api["url"]]) == 0

    fake_api["rows"] = []
    assert verify_storage.main(["--logs", str(tmp_path), "--api", fake_api["url"]]) == 1

    empty = tmp_path / "empty"
    empty.mkdir()
    assert verify_storage.main(["--logs", str(empty), "--api", fake_api["url"]]) == 2
    assert "no message records" in capsys.readouterr().err


def test_unreachable_api_exits_2(tmp_path, capsys):
    write_logs(tmp_path)
    code = verify_storage.main(["--logs", str(tmp_path), "--api", "http://127.0.0.1:1"])
    assert code == 2
    assert "verify_storage:" in capsys.readouterr().err
