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

### MVP and optional storage (Docker Compose)

Run the full Docker acceptance checkpoint (single HOLD, three-follower
broadcast, 5% battery decline, state and both-side JSONL evidence):

```bash
.venv/bin/python -m scripts.run_mvp
```

Or show just the two-service HOLD/COMPLY exchange:

```bash
docker compose up --build --abort-on-container-exit --exit-code-from commander
```

See [the MVP run guide](docs/mvp-running.md) for evidence, cleanup, policy scope,
and manual commands. SQL/.NET storage is optional under `--profile storage`;
see [Docker setup](docs/docker-setup.md), [database](docs/database.md), and
[storage](docs/storage.md).

## Protocol & design docs

The v1 protocol is specified in [`src/schema/falcon-v1.json`](src/schema/falcon-v1.json)
and documented here:

- [`docs/mvp-running.md`](docs/mvp-running.md) — Docker milestone, JSONL evidence, CI and go/no-go checks
- [`docs/heartbeat.md`](docs/heartbeat.md) — opt-in heartbeat timing and RoleManager status/loss callbacks
- [`docs/logging.md`](docs/logging.md) — buffered rotation, dynamic hierarchy levels, role/event records
- [`docs/transport.md`](docs/transport.md) — transport API, in-memory pairs, TCP demo, recovery, and agent handoff
- [`docs/commander.md`](docs/commander.md) — Commander module: building, sending and correlating commands
- [`docs/hierarchy-runtime.md`](docs/hierarchy-runtime.md) — command chain model and dual-role (middle) agents
- [`docs/roles.md`](docs/roles.md) — Role Manager: role requests, grants, epochs and stale authority
- [`docs/failover.md`](docs/failover.md) — deterministic failover: detection, selection, takeover, invalidation
- [`docs/protocol.md`](docs/protocol.md) — message envelope
- [`docs/vocabulary.md`](docs/vocabulary.md) — commands, responses, decline reasons, role/heartbeat messages
- [`docs/state.md`](docs/state.md) — simulated follower state
- [`docs/acceptance-criteria.md`](docs/acceptance-criteria.md) — Given/When/Then per command
- [`docs/versioning.md`](docs/versioning.md) — schema versioning strategy
- [`docs/protocol-errors.md`](docs/protocol-errors.md) — protocol error behavior

## Development workflow

All work happens on feature branches merged into `main` via reviewed pull
requests; `main` is protected and CI must be green. Full conventions
(branch naming, commits, reviews, merge policy) are in
[`docs/git-workflow.md`](docs/git-workflow.md). CI is described in
[`docs/ci.md`](docs/ci.md).

## Status

Phase 0 (foundation, FALCON-001–008) and Phase 1 (protocol definition,
FALCON-009–018) complete: the v1 schema, vocabulary, state model, acceptance
criteria, versioning, and error behavior are defined and schema-tested. The
build follows a layered plan — Foundation → Protocol → single-pair MVP → TCP →
decision engine → logging/DB → role switching → hierarchy → disconnect
recovery → test harness → CI/CD → demo. The MVP round trip (FALCON-031) is the
go/no-go checkpoint. The system is kept runnable after every work session.

TCP transport endpoints and a two-process smoke demo are available; see
[`docs/transport.md`](docs/transport.md#two-process-demo-and-buffering-falcon-028).
Actual Commander/Follower TCP sessions now support stateful commands,
REPORT_STATUS-based registration, and bounded recovery after follower restart.
See the [agent session commands](docs/transport.md#actual-agent-sessions-falcon-028029).
Decision policy remains injectable. Compose uses `src.decision.decide`.

The Docker MVP acceptance gate checks real agents and both-side JSONL logs.
See [the run guide](docs/mvp-running.md) for the checkpoint and decision policy.
