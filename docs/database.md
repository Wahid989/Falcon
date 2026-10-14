# Database (SQL Server)

Persistent storage uses Microsoft SQL Server, run locally via Docker Compose.
This page covers the database setup and schema. For how log data gets into
it and how to query it, see [`storage.md`](storage.md).

## Running SQL Server

The `sql-server` service in [`docker-compose.yml`](../docker-compose.yml) runs
`mcr.microsoft.com/mssql/server:2022-latest`, exposes port `1433`, and reports a
health status once it accepts connections. The `storage-api` service waits for
that health check before starting.

```bash
docker compose up -d sql-server
docker compose ps        # sql-server should become "healthy"
```

The database lives inside the container (there is no named volume), so
`docker compose down` deletes it. The storage service rebuilds it from the
logs in `./logs` on the next start.

## Connection configuration

Configuration is supplied through environment variables — no credentials are
committed. Copy the example file and adjust:

```bash
cp config/.env.example .env     # .env is git-ignored
```

| Variable | Purpose |
| --- | --- |
| `MSSQL_SA_PASSWORD` | SA password. Must meet SQL Server complexity rules (8+ chars, upper, lower, digit, symbol). |
| `ConnectionStrings__FalconDb` | Connection string the .NET storage service uses. Host `sql-server` is the Compose service name. The `Database=` part names the database the service creates and uses. |
| `FALCON_DB` | Database name for `scripts/init-db.sh` and `scripts/test-db.sh` (default `Falcon`). |

Docker Compose reads the root `.env` automatically.

## Initialization strategy

The SQL Server image does not run init scripts by itself, so initialization is
an explicit, idempotent step. Scripts live in
[`storage/sql/init/`](../storage/sql/init) and run in filename order:

| Script | Creates |
| --- | --- |
| `001_create_database.sql` | The database |
| `002_create_exchange_schema.sql` | Exchange-record tables (FALCON-038) |
| `003_create_ingestion_tables.sql` | Ingestion support tables (FALCON-042) |

The database name is the sqlcmd variable `$(FalconDb)`, so the same scripts can
create a separate test database.

There are three ways to apply them. All are safe to repeat.

1. **Automatically.** The storage service applies them at startup, using the
   database named in its connection string.
2. **From the host** (needs `sqlcmd`):
   ```bash
   MSSQL_SA_PASSWORD=... ./scripts/init-db.sh
   ```
3. **Inside the container.** `storage/sql` is mounted read-only at `/storage/sql`:
   ```bash
   docker compose exec -T sql-server /bin/bash < scripts/init-db.sh
   ```

## Schema

`hierarchy_level` (the `HierarchyLevel` column) is **log-only**: an agent's
distance from the current top commander (0 = top). It is stored because the
agents write it in their logs. It is never on the wire. It is nullable, and
NULL means the level was unknown, for example for a message from an
unrecognized sender. Every table that has it is indexed on it.

### Exchange record (`002_create_exchange_schema.sql`)

| Table | One row per… | Notes |
| --- | --- | --- |
| `Sessions` | run of the system | `StartedAt` / `EndedAt` = first and last record seen. |
| `Messages` | wire message **per logging agent** | The envelope fields plus `Direction` (`SENT`/`RECEIVED`), `LoggedBy`, `HierarchyLevel` and `Payload` (JSON). One HOLD normally gives two rows: SENT in the commander's log, RECEIVED in the follower's. `MessageId` is not unique, because a re-sent message is a real event. |
| `StateSnapshots` | copy of one agent's simulated state | `State` is JSON, so the state model can grow without a schema change. |
| `RoleHandovers` | role transition or decision | Requester, target commander (shows skip-level requests), previous commander, old/new role, old/new epoch, `Outcome`, `Reason`. |

`RoleHandovers.Outcome` and the epoch rules the database enforces:

| Outcome | Meaning | Epoch | Reason |
| --- | --- | --- | --- |
| `GRANTED` | commander granted a role request | must increase | optional |
| `DENIED` | commander refused a role request | unchanged | required |
| `SELF_PROMOTED` | failover: designated survivor took over | must increase | optional |
| `STEPPED_DOWN` | revived commander rejoined as a follower | must increase (adopts the newer epoch) | optional |
| `PROMOTION_DECLINED` | failover: survivor failed its live re-check | unchanged | required |
| `VACANT` | failover: no fit agent, waiting for a human | unchanged | optional |

### Ingestion support (`003_create_ingestion_tables.sql`)

| Table | Purpose |
| --- | --- |
| `Events` | Error and event records from the logs (severity, type, JSON detail). |
| `IngestionRejects` | Every log line that could not be loaded, with file, line number and reason. Nothing is dropped silently. |
| `IngestionCursors` | How far each log file has been read. |

### Keys and indexes

- `SourceHash` (unique, on every data table) identifies the log line a row came
  from: its file, its position in that file and its content. It is what keeps
  ingestion from ever creating a duplicate row.
- `IX_*_HierarchyLevel` exists on `Messages`, `StateSnapshots` and `RoleHandovers`.
- Session/time indexes support timelines and summaries. `(AgentId, Timestamp)`
  serves state history, and `MessageId` finds both sides of one message.
- Foreign keys from every data table go to `Sessions`. Every timestamp is
  `DATETIME2(6)` in UTC.

## Testing

```bash
docker compose up -d sql-server
./scripts/test-db.sh
```

This applies every init script twice, to prove they are idempotent, and then
runs `storage/sql/tests/*.sql`:
- valid rows are accepted;
- each constraint rejects a bad row;
- the `HierarchyLevel` columns and indexes exist;
- level filtering works.

The tests run in a transaction that is rolled back, so they leave no data
behind.

## Verifying connections

```bash
docker compose exec sql-server /opt/mssql-tools18/bin/sqlcmd \
  -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -No -Q "SELECT name FROM sys.databases"
```

`Falcon` should appear in the list after initialization.
