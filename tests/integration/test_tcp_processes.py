"""Independent Python processes exchanging schema-valid demo messages."""

import json
from pathlib import Path
import subprocess
import sys

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[2]


def test_two_process_round_trip():
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "scripts.tcp_demo",
            "follower",
            "--port",
            "0",
            "--timeout",
            "3",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # The child's finite accept timeout also bounds an unsuccessful readiness read.
        line = server.stdout.readline()
        assert line, server.stderr.read()
        address = json.loads(line)["listening"]
        client = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.tcp_demo",
                "commander",
                "--port",
                str(address["port"]),
                "--timeout",
                "2",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert client.returncode == 0, client.stderr
        output, error = server.communicate(timeout=5)
        assert server.returncode == 0, error
        command = json.loads(output)["received"]
        response = json.loads(client.stdout)["received"]
        schema = json.loads((ROOT / "src/schema/falcon-v1.json").read_text())
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        validator.validate(command)
        validator.validate(response)
        assert command["payload"] == {"command": "HOLD"}
        assert response["payload"] == {
            "response": "COMPLY",
            "in_reply_to": command["message_id"],
        }
    finally:
        if server.poll() is None:
            server.kill()
        server.communicate(timeout=2)
