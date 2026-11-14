# Storage service (log ingestion + query API)

The .NET storage service (`storage/dotnet`) loads the agents' JSONL exchange
logs into SQL Server and serves them through a read-only REST API. The
database schema is described in [`database.md`](database.md).

## Data flow

```
 Agent processes (Python)                    storage-api (.NET 8)                SQL Server
 ┌───────────────────────┐   ./logs/*.jsonl   ┌──────────────────────────┐        ┌────────────────┐
 │ Commander / Follower  │ ─────────────────▶ │ LogIngester (every 10 s, │ ─────▶ │ Messages       │
 │ JSONL log writer      │   files only;      │  or POST /api/ingest)    │        │ StateSnapshots │
 └───────────────────────┘   no calls         │ LogRecordParser          │        │ RoleHandovers  │
                                              │ REST API  (/api/...)     │ ◀───── │ Events ...     │
                                              │ OpenAPI   (/swagger)     │        └────────────────┘
                                              └──────────────────────────┘
```

The Python runtime never calls the storage service or the database. Agents
only append lines to their log files. If storage is down, the agents carry on
unaffected, and the backlog is loaded when storage comes back.

1. An agent sends or receives a message and appends one JSON line to its log,
   with `direction` and the log-only `hierarchy_level`.
2. The ingester reads new, complete lines from every `*.jsonl*` file in the log
   directory **and its subfolders**, so per-session folders such as
   `logs/mvp/<session>/` are picked up automatically.
3. Each line is validated. A valid record goes to its table. An invalid line
   goes to `IngestionRejects` with the reason.
4. The rows and the file's new read position commit in one transaction.
5. The API serves the stored records, filterable by `hierarchy_level`.

## Running

```bash
docker compose --profile storage up -d --build sql-server storage-api
curl http://localhost:8080/health          # Healthy
```

The storage services run under the Compose `storage` profile.

> **Known issue:** the shared `falcon` network is `internal: true`, which keeps
> the agents offline but also stops Docker publishing ports 8080 and 1433, so
> the API and Swagger can't be reached from the host. Until the storage
> services get a non-internal network of their own, reach the API from a
> container on `falcon_falcon` (as in the verification command below).

Interactive API docs: <http://localhost:8080/swagger>.

At startup the service applies `storage/sql/init` (see `database.md`), then
reads `./logs` (mounted read-only at `/logs`) every 10 seconds.

| Setting (environment) | Default | Meaning |
| --- | --- | --- |
| `ConnectionStrings__FalconDb` | see `docker-compose.yml` | SQL Server connection; its `Database=` is created if missing. |
| `Storage__LogDirectory` | `/logs` | Directory of JSONL logs, searched recursively. |
| `Storage__LogFilePattern` | `*.jsonl*` | Log files, including rotated copies. |
| `Storage__IngestIntervalSeconds` | `10` | Background interval; `0` disables it. |
| `Storage__BatchSize` | `500` | Lines per transaction. |
| `Storage__SchemaScriptsPath` | `/app/sql/init` | Init scripts applied at startup; empty skips. |

## REST API

| Endpoint | Purpose | Query parameters |
| --- | --- | --- |
| `GET /api/messages` | Exchange messages, oldest first | `sessionId`, `hierarchyLevel` (integer, or `null` for unknown), `senderId`, `receiverId`, `type`, `direction`, `limit` (1–1000, default 100), `offset` |
| `GET /api/handovers` | Role handovers and decisions | `sessionId`, `hierarchyLevel`, `outcome`, `limit`, `offset` |
| `GET /api/state/{agentId}/history` | State snapshots for one agent | `sessionId`, `limit`, `offset` |
| `GET /api/sessions` | All sessions, newest first | |
| `GET /api/session/{id}/summary` | Counts, agents, messages per type and per level | 404 if unknown |
| `POST /api/ingest` | Run an ingestion pass now | |
| `GET /health` | Liveness | |

List endpoints return `{ "items": [...], "total": N, "limit": L, "offset": O }`.
Invalid parameters return HTTP 400 with the reason.

## Log record format

One JSON object per line, ending in `\n`. Common fields:
- `record_type`: `message`, `state_snapshot`, `role_change` or `event`;
- `logged_by`;
- `logged_at`;
- `session_id`, optional;
- `hierarchy_level`: an integer, or `null`.

A `message` record example:

```json
{"record_type":"message","session_id":"run-42","logged_by":"ground",
 "logged_at":"2026-10-13T12:00:01.005Z","direction":"sent","hierarchy_level":0,
 "message":{"message_id":"m-1","sender_id":"ground","recipient_id":"drone-cmd",
            "timestamp":"2026-10-13T12:00:01Z","schema_version":"1.0",
            "sender_role":"commander","role_epoch":1,"type":"command",
            "payload":{"command":"HOLD"}}}
```

What each record type needs:

| Record type | Required fields |
| --- | --- |
| `message` | `direction` (`sent`/`received`) and the full wire envelope in `message` |
| `state_snapshot` | `state` (object) |
| `role_change` | `requester_id`, `old_epoch`, `new_epoch` and `outcome`; epoch rules as in `database.md` |
| `event` | `event_type`; optional `severity` and `detail` |

`hierarchy_level` follows FALCON-040. For a sent message it is the logging
agent's own level. For a received message it is the sender's level, or `null`
if the sender is unknown.

This format is the storage side's proposal, to be confirmed with the log
writer (FALCON-039–041) and the envelope (FALCON-014). The parser is
`storage/dotnet/src/FalconStorage/Ingestion/LogRecordParser.cs`.

**Rules for the writer**
- Append only.
- Rotate by renaming the whole file, then start a new one.
- End every line with a newline.

**Guarantees**

| Situation | Behavior |
| --- | --- |
| A file is read again (restart, crash, two service instances) | No duplicate rows. Rows are keyed by file, line position and content. |
| A file is renamed during rotation | The ingester resumes where it stopped. Files are recognized by their first non-blank line, not their name. |
| The writer is mid-line | The partial line is read once it is complete. |
| The service fails mid-batch | The whole batch rolls back, together with the read position, and is retried. |
| A line is bad | It is stored in `IngestionRejects` with the reason. The lines after it still load. |
| Two identical lines appear (for example a re-sent message) | They stay two rows. |

## Checking that every exchange reached the database

`scripts/verify_storage.py` compares a run's logs with the database. It:
- reads every message record under a folder;
- asks the service to ingest;
- checks that each record is stored with the same message, agent, direction
  and `hierarchy_level`.

It prints a report and exits `0` when nothing is missing or extra.

```bash
# After a run (e.g. python -m scripts.run_mvp), with the storage profile up.
# The Compose network is internal, so run it on that network:
MSYS_NO_PATHCONV=1 docker run --rm --network falcon_falcon -v "$PWD:/repo:ro" -w /repo   falcon-agent python -m scripts.verify_storage   --logs logs/mvp/<session> --api http://storage-api:8080
```

`--no-ingest` skips the ingest request, to check that the background pass
picked the run up on its own.

## Tests

```bash
# SQL schema tests
./scripts/test-db.sh

# .NET unit + integration tests, using a separate FalconTest database
MSYS_NO_PATHCONV=1 docker run --rm -v "$PWD/storage:/work" -w /work/dotnet \
  --network falcon_falcon \
  -e FALCON_TEST_DB="Server=sql-server,1433;Database=FalconTest;User Id=sa;Password=Falcon_local_dev_1;TrustServerCertificate=True" \
  mcr.microsoft.com/dotnet/sdk:8.0 dotnet test
```

With the .NET 8 SDK installed, run `dotnet test storage/dotnet` with
`FALCON_TEST_DB` pointing at `localhost,1433`.

`AgentLogIngestionTests` ingest logs written by the real Python Commander and
Follower (`tests/FalconStorage.Tests/Fixtures/agent-logs`, in a nested
folder). They check that:
- every exchange is stored once as SENT and once as RECEIVED;
- `hierarchy_level` is correct on each row;
- re-ingesting adds nothing.

`tests/integration/test_exchange_log_content.py` checks that the fixture still
matches what the log writer produces. If the format changes, regenerate it with
`run_exchange()` from that test.

The database tests are skipped when `FALCON_TEST_DB` is not set. Set
`FALCON_REQUIRE_DB=1` to make a missing database fail the run instead (for
CI). The tests cover:
- parsing;
- idempotency;
- rotation;
- partial lines;
- concurrency;
- rollback;
- per-file failure isolation;
- every API endpoint and filter.
