# Continuous Integration

CI runs on GitHub Actions. The workflow is defined in
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

## When it runs

- On **every push** to any branch.
- On **every pull request**.

Status (pass/fail) is reported on the commit and on the PR.

## What it checks

**Python job (`python`)**
- Sets up Python 3.12.
- Installs `requirements.txt` / `requirements-dev.txt` when present, and always
  installs `pytest`, `black`, and `flake8`.
- `black --check .` — fails if any file is not formatted.
- `flake8 .` — lints (config in [`.flake8`](../.flake8), aligned with black).
- `timeout 30s python -m pytest tests/ -v` — runs the test suite, including
  real loopback TCP/process tests, with a 30-second budget (GNU timeout on Ubuntu).

**.NET job (`dotnet`)**
- Sets up .NET 8.
- Restores and builds `storage/dotnet` **only if** a `.csproj`/`.sln` exists.
  Until the .NET service is scaffolded, this job passes without building.

**Docker MVP job (`mvp`)**
- Builds and runs `python -m scripts.run_mvp` using Docker Compose.
- Requires single HOLD/COMPLY, broadcast to three, battery exactly 5% decline,
  state preservation, schema/correlation, and both-side JSONL evidence.
- Uploads `logs/mvp/` as the `mvp-evidence` artifact, including on failures.
- Uses a 10-minute job budget including builds; per-command timeout is 5 seconds.
- See [mvp-running.md](mvp-running.md) for reproduction and report interpretation.

## Making a failing check block merges

The workflow reports status, but GitHub only *blocks* a merge when branch
protection requires the check. To enable it (repo admin):

1. **Settings → Branches → Add branch ruleset** (or classic branch protection)
   targeting `main`.
2. Enable **Require a pull request before merging** and **Require status checks
   to pass before merging**.
3. Add `Python (lint + tests)` and `.NET (build)` as required checks.

Also require `Docker MVP checkpoint` to block merges on failed milestone checks.
Once set, a red required check on a PR prevents merging into `main`.

## Running the checks locally

```bash
pip install pytest black flake8
black --check .
flake8 .
python -m pytest tests/ -v
```

Run from the repository root with the project virtual environment active.
Using `python -m pytest` includes the repository root on Python's import path,
so tests can import `src.<package>`. This matches the `make test` command.

## Windows transport regressions

The independent `Windows transport regression tests` job uses Python 3.12 and
the pinned dependencies on windows-latest. It runs the socket regression file
and the graceful idle Follower shutdown test. The existing Linux job remains
unchanged. Windows termination uses CTRL_BREAK_EVENT in a new process group;
Popen.terminate() is a forced kill there and cannot test Python cleanup.

## Storage integration (043)

`Storage integration (agents + SQL)` runs independently on Ubuntu with Python
3.12, .NET 8 and a fresh SQL Server 2022 container. It generates logs using the
real Commander and Follower integration helper (HOLD, low-battery RETURN decline,
REPORT_STATUS, plus an excluded heartbeat), then starts the storage API against
those files. It runs the SQL scripts and the full .NET suite, including the
existing constraints, concurrency, rollback, rotation and ingestion tests.
`FALCON_REQUIRE_DB=1` and an explicit test connection prevent database tests
from silently skipping. The test database is separate from the API database.

Finally, two `scripts.verify_storage` passes require all twelve message records
(six wire messages, sender and receiver copies) to be stored with their original
agent, direction and hierarchy level, without extra rows on re-ingestion. The
Python job separately checks the live format against the committed .NET fixture,
role/error records and dynamic levels after grants.

The isolated Compose project is removed even on failure. JSON comparison reports,
TRX results, fresh logs and Compose output are uploaded as
`storage-integration-evidence`. Add `Storage integration (agents + SQL)` to main's
required checks after its first successful run; changing branch protection is a
repository-admin action, not part of the workflow file.

This job verifies log-to-database compatibility using in-memory agent transport;
the separate Docker MVP job verifies real TCP exchanges. Neither claims to run
the later Phase 9 depth/scenario matrix.
