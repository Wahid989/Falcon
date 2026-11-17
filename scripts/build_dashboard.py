#!/usr/bin/env python3
"""Assemble the static dashboard into ./site for GitHub Pages.

Runs in CI after the test step. Degrades gracefully: whatever inputs exist get
rendered; whatever doesn't is marked "not available yet". This lets the page
ship today (status only) and fill in automatically once the scenario harness
(FALCON-066/084) starts producing a run log.

Inputs it looks for (all optional):
  - test-results.xml   (JUnit, from `pytest --junitxml=test-results.xml`)
  - coverage.xml       (from `pytest --cov --cov-report=xml`)
  - run.jsonl          (one JSON exchange per line, from the scenario harness)
Outputs:
  - site/index.html        (copied from dashboard/index.html)
  - site/data/summary.json (what index.html reads)
  - site/data/run.jsonl    (copied if present)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
DATA = SITE / "data"


def git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
    except Exception:
        return ""


def parse_junit(path: Path):
    if not path.exists():
        return None
    try:
        root = ET.parse(path).getroot()
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        tests = sum(int(s.get("tests", 0)) for s in suites)
        failures = sum(int(s.get("failures", 0)) for s in suites)
        errors = sum(int(s.get("errors", 0)) for s in suites)
        skipped = sum(int(s.get("skipped", 0)) for s in suites)
        passed = tests - failures - errors - skipped
        return {
            "total": tests,
            "passed": passed,
            "failed": failures + errors,
            "skipped": skipped,
            "green": (failures + errors) == 0,
        }
    except Exception:
        return None


def parse_coverage(path: Path):
    if not path.exists():
        return None
    try:
        root = ET.parse(path).getroot()
        rate = root.get("line-rate")
        return round(float(rate) * 100, 1) if rate is not None else None
    except Exception:
        return None


def count_jsonl(path: Path):
    if not path.exists():
        return None
    try:
        return sum(1 for line in path.read_text().splitlines() if line.strip())
    except Exception:
        return None


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)

    tests = parse_junit(ROOT / "test-results.xml")
    coverage = parse_coverage(ROOT / "coverage.xml")

    run_log = ROOT / "run.jsonl"
    run_exchanges = count_jsonl(run_log)
    if run_log.exists():
        shutil.copy(run_log, DATA / "run.jsonl")

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit": git("rev-parse", "--short", "HEAD"),
        "commit_msg": git("log", "-1", "--pretty=%s"),
        "branch": (
            git("rev-parse", "--abbrev-ref", "HEAD") or os.getenv("GITHUB_REF_NAME", "")
        ),
        "tests": tests,  # None if no test report
        "coverage_pct": coverage,  # None if no coverage report
        "run_exchanges": run_exchanges,  # None until a scenario produces run.jsonl
    }
    (DATA / "summary.json").write_text(json.dumps(summary, indent=2))

    # copy the page shell
    src_index = ROOT / "dashboard" / "index.html"
    shutil.copy(src_index, SITE / "index.html")
    print("Built site/ ->", json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
