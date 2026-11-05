"""Build and verify an isolated Docker Compose MVP, retain evidence, then clean up."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("follower", "follower-2", "follower-3", "follower-low")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-build", action="store_true", help="use an already built agent image"
    )
    parser.add_argument(
        "--output", default="logs/mvp", help="parent directory for unique run evidence"
    )
    args = parser.parse_args(argv)
    session = uuid4().hex
    directory = Path(args.output).resolve() / session
    directory.mkdir(parents=True)
    env = {
        **os.environ,
        "FALCON_SESSION_ID": session,
        "FALCON_LOG_ROOT": str(directory),
    }
    compose = [
        "docker",
        "compose",
        "-f",
        str(ROOT / "docker-compose.yml"),
        "-p",
        f"falcon-mvp-{session[:12]}",
        "--profile",
        "broadcast",
        "--profile",
        "decline",
    ]
    report_path = directory / "mvp-report.json"
    code = 1
    try:
        if not args.no_build:
            subprocess.run(
                compose + ["build", "commander"],
                cwd=ROOT,
                env=env,
                check=True,
                timeout=600,
            )
        subprocess.run(
            compose + ["up", "-d", "--no-build", *SERVICES],
            cwd=ROOT,
            env=env,
            check=True,
            timeout=120,
        )
        command = compose + [
            "run",
            "--rm",
            "--no-deps",
            "commander",
            "python",
            "-m",
            "scripts.mvp_checkpoint",
            "--log-dir",
            "/logs",
            "--session-id",
            session,
            "--report",
            "/logs/mvp-report.json",
        ]
        for name, service in zip(("follower-1", *SERVICES[1:]), SERVICES):
            command += ["--follower", f"{name}={service}:5000"]
        subprocess.run(command, cwd=ROOT, env=env, check=True, timeout=120)
        report = json.loads(report_path.read_text())
        if report.get("decision") != "GO" or report.get("session_id") != session:
            raise ValueError("Checkpoint evidence did not confirm this run")
        code = 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if not report_path.exists():
            report_path.write_text(
                json.dumps(
                    {"decision": "NO_GO", "session_id": session, "error": str(exc)},
                    indent=2,
                )
                + "\n"
            )
        print(f"MVP failed: {exc}", file=sys.stderr)
    finally:
        try:
            logs = subprocess.run(
                compose + ["logs", "--no-color"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            (directory / "compose.log").write_text(logs.stdout + logs.stderr)
            cleanup = subprocess.run(
                compose + ["down", "--timeout", "5"], cwd=ROOT, env=env, timeout=30
            )
            if cleanup.returncode:
                code = 1
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"Cleanup failed: {exc}", file=sys.stderr)
            code = 1
        print(f"Evidence: {directory}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
