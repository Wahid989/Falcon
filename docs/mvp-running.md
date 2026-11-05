# Running and verifying the MVP (FALCON-031)

This guide covers Munem's plumbing: the real Commander/Follower runtime in
Docker Compose, TCP exchange logging on both sides, the acceptance checkpoint,
and CI. The wire contract is in [mvp-roundtrip.md](mvp-roundtrip.md); transport
and recovery are in [transport.md](transport.md).

## One command: full milestone acceptance

Start Docker Desktop. From the repository root:

```bash
.venv/bin/python -m scripts.run_mvp
```

The host runner uses only the Python standard library, so `python3 -m
scripts.run_mvp` also works without the development virtual environment.
The first image build downloads Python and the existing pinned requirements.
After the image is built, the agent runtime uses an isolated internal Compose
network and no Internet services. No SQL Server or .NET service is required.

The runner creates a uniquely named Compose project and a fresh session ID,
starts four Follower containers, and runs the actual Commander inside a fifth
container to verify all acceptance cases. It then removes only its own containers
and network. Logs and the report remain on the host. For subsequent runs with
unchanged source/image, skip building:

```bash
.venv/bin/python -m scripts.run_mvp --no-build
```

Rebuild after changing code. Do not use `--no-build` to verify unbuilt changes.

Successful output includes `"decision": "GO"` and six true checks:

The terminal report uses indented JSON with line breaks and a blank line before
and after it for easy reading and recording. The saved `mvp-report.json` is also
indented; exchange `*.jsonl` files retain one complete record per line.

| Check | What is actually verified |
| --- | --- |
| single_hold | A moving follower receives HOLD, returns COMPLY within the configured timeout, and clears movement/current command. |
| broadcast_three | Commander.broadcast sends individually addressed HOLD envelopes to follower-1/2/3 and receives three COMPLY responses within one deadline. |
| battery_5_decline | A separate follower starts at exactly 5% battery; RETURN produces DECLINE/BATTERY_CRITICAL. |
| state_preserved | Before/after STATUS_RESPONSE snapshots prove decline leaves every reported field unchanged; HOLD changes only its expected movement/command fields. |
| schema_and_correlation | Every accepted command/response validates, comes from its expected peer, and replies using the correct in_reply_to. |
| both_sides_logged | Every wire message has exactly one matching sent record and one received record in the correct agent files for this fresh session. |

The checkpoint sends status queries as well as the required commands, producing
28 distinct wire envelopes and 56 message log records across five files. It
reports single-HOLD latency, the maximum measured round trip, three-follower
broadcast latency, and the 5-second timeout. Timings exclude image build,
container startup, connection setup, and later evidence validation. There is no
additional sponsor latency target assumed beyond completing within the timeout.

Any missing response, wrong outcome, schema/correlation/state failure, missing or
duplicate log receipt, or startup failure results in exit status 1 and NO_GO.
A previous GO cannot pass a new run: session IDs, projects, and directories are
unique. Exit 0 plus GO is the automated acceptance recommendation for this MVP
scope; review the evidence before the team makes its milestone decision.

## Evidence you can show the TA

The runner prints its evidence directory, for example `logs/mvp/<session-id>/`:

```text
commander.jsonl
follower-1.jsonl
follower-2.jsonl
follower-3.jsonl
follower-low.jsonl
mvp-report.json
compose.log
```

Open `mvp-report.json` for the acceptance results, timings, and complete
low-battery before/after snapshots. Open the JSONL files to compare a command's
message_id on both sides and its response's payload.in_reply_to. `compose.log`
contains each Follower's received request, sent response, and post-command state.
Logs are runtime evidence and are git-ignored. They are not committed test data.

## Minimal two-service demonstration

Compose followers display requests, responses, and state as indented JSON with
blank lines between events. Rebuild/recreate with `docker compose up --build
follower` to apply changes to an existing container. For a local non-Docker
Follower, add `--pretty` or set FALCON_PRETTY_OUTPUT=1. Machine-oriented local
output remains one JSON object per line by default; exchange JSONL files always
retain one record per line regardless of terminal display settings.

This starts only the Commander and one Follower, sends HOLD, prints COMPLY,
and stops both when the one-shot Commander exits:

```bash
docker compose up --build --abort-on-container-exit --exit-code-from commander
```

Expected Commander output contains `"outcome": "COMPLY"` and a summary of
one answered follower. This simple command is a smoke test, not the full gate.
Its default logs are `logs/commander.jsonl` and `logs/follower-1.jsonl`, appended
under the session `manual-mvp`. Remove the stopped containers/network afterward:

```bash
docker compose down
```

To keep the follower running and send commands separately:

```bash
docker compose up -d --build follower
docker compose run --rm --no-deps commander
docker compose run --rm --no-deps commander python -m src.agents.commander_runner --command REPORT_STATUS
```

The Commander resolves Compose service names above the transport layer. TCP
backends remain IPv4/localhost only. Host ports are not exposed for the agent
services; communication is internal to the Compose project.

## Manual broadcast and decline

Start optional followers with their profiles:

```bash
docker compose --profile broadcast --profile decline up -d --build follower follower-2 follower-3 follower-low
```

Broadcast to the three healthy followers:

```bash
docker compose run --rm --no-deps -e FALCON_FOLLOWERS=follower-1=follower:5000,follower-2=follower-2:5000,follower-3=follower-3:5000 commander
```

Run a movement command against the low-battery follower:

```bash
docker compose run --rm --no-deps -e FALCON_FOLLOWERS=follower-low=follower-low:5000 commander python -m src.agents.commander_runner --command RETURN
```

Expect DECLINE/BATTERY_CRITICAL. HOLD is intentionally allowed at low battery;
use RETURN or PROCEED_TO_WAYPOINT for the decline path. For an independent manual
state check, run REPORT_STATUS before and after RETURN and compare state objects.
The full acceptance runner does this automatically. Clean up these services with:

```bash
docker compose --profile broadcast --profile decline down
```

## Logging API and behavior

`src/logging/exchange.py` provides an optional `ExchangeLogger`, injected into
the actual Commander/Follower modules. Transport remains unaware of logs.
Commander and Follower log successful sends and decoded receives, including
registration/status traffic, in the storage ingester's JSONL message format:

```json
{"record_type":"message","session_id":"run-id","logged_by":"commander","logged_at":"2026-11-04T12:00:00Z","direction":"sent","hierarchy_level":0,"message":{"message_id":"...","sender_id":"commander","recipient_id":"follower-1","timestamp":"2026-11-04T12:00:00Z","schema_version":1,"sender_role":"commander","role_epoch":0,"type":"command","payload":{"command":"HOLD"}}}
```

CLI options `--log-dir` and `--session-id` override FALCON_LOG_DIR and
FALCON_SESSION_ID. Logging is optional outside Compose; Compose enables it by
default. FALCON_LOG_ROOT selects the host bind directory (default `./logs`).

- Every record ends with a newline and is appended; existing files are preserved.
- Each write closes/flushed its file before returning. There is no per-write
  fsync guarantee, crash-atomic filesystem transaction, cross-process file lock,
  or rotation in this milestone. Use one process/writer per agent file.
- Heartbeats are excluded. hierarchy_level is log-only: own level for sent
  messages, known sender level for receives, or null if unknown. The MVP is a
  flat hierarchy (Commander 0, all followers 1); dynamic enrichment is later work.
- Logging failures are visible and fail the CLI/checkpoint; evidence is not
  silently discarded. A send may already have happened before its log fails,
  so this is not permission to replay the command.
- These are exchange logs only. Full event/state/role logs, rotation, and dynamic
  hierarchy enrichment remain FALCON-039–041. The record shape matches the .NET
  ingestion contract, but SQL ingestion is not required or certified by this gate.

## Decision policy and scope

Compose uses the team's `src.decision.engine:decide` MVP implementation. It
complies with HOLD/REPORT_STATUS and declines movement when battery is below
20%, without mutating state. The real Follower performs validation, authority
checks and state transitions. The full Phase-4 terrain/capability/mechanical/
availability policy remains separate from this MVP decision slice.

`--decision-engine MODULE:CALLABLE` can override the runner's default for explicit
experiments. `scripts.tcp_agent_policy:decide` remains an optional older demo
helper; it is not used by the Compose milestone gate.

Normal followers start moving via `config/mvp/moving.yml` so HOLD has a visible
effect. The low follower uses `config/mvp/low-battery.yml`, which sets battery to
5 and position to north-fence. The older `tests/scenarios/follower_low_battery.yml`
sets 15%, so it is not used for this exact milestone requirement.

Storage/API services are preserved under the optional `storage` profile:

```bash
docker compose --profile storage up -d --build sql-server storage-api
```

The agent image is built from the repo root and copies only runtime source,
scripts, requirements and MVP configuration. The image does not include local
virtual environments or credentials. The SQL/.NET containers remain independent
of agent execution. For evidence in a nested session directory, configure the
storage service's log directory explicitly before ingesting that run.

## Tests and CI

```bash
make test
make lint
.venv/bin/python -m scripts.run_mvp
```

Python tests include an equivalent live TCP checkpoint and negative evidence
checks; Docker is not required for `make test`. The existing 30-second Python
suite budget stays in place. A new independent GitHub Actions job, **Docker MVP
checkpoint**, builds and runs the actual Compose gate and uploads `logs/mvp/` as
`mvp-evidence`, even when the checkpoint fails. The job has a 10-minute budget
including builds; this is separate from command-response latency.

For branch protection, require Docker MVP checkpoint alongside Python and .NET
checks if the team wants milestone failures to block merges. The workflow is
configured here; remote CI status must be checked after pushing.

New files: `.dockerignore`, `src/logging/exchange.py`, the two `config/mvp`
scenarios, `scripts/mvp_checkpoint.py`, `scripts/run_mvp.py`, and logging/checkpoint
tests. Updated files: agent logging injection, CLI configuration, agent Dockerfile,
Compose services/profiles, CI, README and linked guides. This branch also preserves
the preceding uncommitted 028/029 runtime integration on which the MVP depends.

## Recorded local acceptance result

Earlier local validation before integrating the newly merged team decision policy
was recorded on Docker Desktop (Linux ARM64 engine):

- Full Python suite: 403 passed in 4.45 seconds; Black and Flake8 passed.
- Default two-service Compose command: exit 0, HOLD/COMPLY, both sides logged.
- Final isolated Compose gate: GO, all six acceptance checks true.
- Single HOLD: 8.552 ms; three-follower broadcast: 11.886 ms; configured limit: 5 s.
- Evidence: 28 wire envelopes / 56 matching sent-and-received JSONL records.
- Low-battery follower: exactly 5%; RETURN declined; all reported state preserved.
- Session: `0fb8b2bfd2c14a6d8282115475fe2243`; evidence is locally retained at
  `logs/mvp/0fb8b2bfd2c14a6d8282115475fe2243/` and is git-ignored.
- Test containers/networks were removed; no unrelated Docker project was changed.

These are observed local results, not a claim that remote CI has already run.
CI is wired and will independently reproduce the checkpoint after a push.
Timings vary by machine; rerun the gate for a fresh decision and evidence.

Before publishing the PR, the latest main branch policy and precise Commander
latency reporting were integrated. The final GO/test results for that combined
implementation are recorded in the PR and its fresh JSON report, rather than
reinterpreting the earlier historical run above.

Final pre-PR combined verification (team MVP policy from latest main):
429 Python tests passed in 4.43 seconds; Black/Flake8 passed. The freshly rebuilt
Docker gate returned GO for session `30c0c056617140f69b2b7eb7b6a45c3c` with all
six checks true, HOLD 10.756 ms, maximum round trip 19.691 ms, broadcast 11.478 ms,
and 28 envelopes / 56 JSONL records. Its policy is `src.decision.decide`.
