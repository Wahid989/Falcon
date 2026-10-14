# FALCON

> A bounded, fully offline command/response protocol between software agents arranged in a dynamic command hierarchy — with role switching, heartbeat-based disconnect recovery, structured logging, and persistent storage.

FALCON is built from scratch as a JSON-over-TCP protocol in which commanders
issue a fixed vocabulary of commands to followers, each of which complies or
declines with an enumerated reason. Agents form a chain hierarchy: every agent
carries both a Commander and a Follower module, so a middle agent is a follower
to its parent and a commander to its own followers. Roles can switch (including
skip-level requests), and a silent commander is detected by heartbeat and
replaced deterministically. The system runs entirely offline — no cloud, no
internet, no external language model, no hardware (the follower's world is
simulated).

## Project

- **Course:** ENEL 500 A/B Capstone — Software Engineering
- **Sponsor:** Tykans Group Inc.

## Team

| Name | Workstream |
| --- | --- |
| Wahid Chowdhury | Protocol & schema — message meaning and authority |
| Munem Morshed | Transport, logging & CI — how agents communicate, record, and ship |
| Maliha Chowdhury Adrita | Follower, decision & testing — how an agent decides and how we prove it |
| Himel Paul | Commander, hierarchy & storage — how commanders drive the tree and store it |

## Repository layout

```
src/
  agents/       Commander and Follower agent runtimes
  schema/       Message envelope, JSON Schema, and validator
  transport/    Transport interface and TCP implementation
  decision/     Capability-aware decision engine
  roles/        Role Manager, role state machine, epochs
  state/        Simulated follower state model and transitions
  logging/      JSONL exchange-log writer and rotation
  services/     Shared cross-cutting helpers
tests/          unit / integration / e2e / scenarios
storage/
  dotnet/       .NET log-ingestion service and query API
  sql/          SQL Server schema and init scripts
scripts/        Developer/ops helpers
config/         Example configuration (.env.example)
docs/           Project documentation
```

See [`docs/repo-structure.md`](docs/repo-structure.md) for details.

## Getting started

### Python (agents, tests)

Requires Python 3.12+. Everything runs through a virtual environment created by
the Makefile:

```bash
make install        # create .venv and install pinned dependencies
make test           # run the test suite
make lint           # black --check + flake8
make format         # auto-format with black
```

The `run-commander` / `run-follower` targets become usable once the agent
runtimes land (Phase 2).

### Full stack (Docker Compose)

Local development of the whole system (agents + .NET storage + SQL Server) runs
through Docker Compose. See [`docs/docker-setup.md`](docs/docker-setup.md),
[`docs/database.md`](docs/database.md) and [`docs/storage.md`](docs/storage.md).

```bash
cp config/.env.example .env     # configure secrets locally (git-ignored)
docker compose up --build
```

## Development workflow

All work happens on feature branches merged into `main` via reviewed pull
requests; `main` is protected and CI must be green. Full conventions
(branch naming, commits, reviews, merge policy) are in
[`docs/git-workflow.md`](docs/git-workflow.md). CI is described in
[`docs/ci.md`](docs/ci.md).

## Status

Phase 0 (project & repository foundation, FALCON-001–008) in progress. The
build follows a layered plan — Foundation → Protocol → single-pair MVP → TCP →
decision engine → logging/DB → role switching → hierarchy → disconnect
recovery → test harness → CI/CD → demo. The MVP round trip (FALCON-031) is the
go/no-go checkpoint. The system is kept runnable after every work session.
