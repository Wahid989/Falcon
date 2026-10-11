# Database (SQL Server)

Persistent storage uses Microsoft SQL Server, run locally via Docker Compose.
This page covers the development setup; table schemas are designed in
FALCON-038 and ingestion in FALCON-042.

## Running SQL Server

The `sql-server` service in [`docker-compose.yml`](../docker-compose.yml) runs
`mcr.microsoft.com/mssql/server:2022-latest`, exposes port `1433`, and reports a
health status once it accepts connections. The `storage-api` service waits for
that health check before starting.

```bash
docker compose up -d sql-server
docker compose ps        # sql-server should become "healthy"
```

## Connection configuration

Configuration is supplied through environment variables — no credentials are
committed. Copy the example file and adjust:

```bash
cp config/.env.example .env     # .env is git-ignored
```

| Variable | Purpose |
| --- | --- |
| `MSSQL_SA_PASSWORD` | SA password. Must meet SQL Server complexity rules (8+ chars, upper, lower, digit, symbol). |
| `ConnectionStrings__FalconDb` | Connection string the .NET storage service uses. Host `sql-server` is the Compose service name. |

Docker Compose reads the root `.env` automatically.

## Initialization strategy

The SQL Server image does not auto-run init scripts, so initialization is an
explicit, idempotent step. Scripts live in
[`storage/sql/init/`](../storage/sql/init) and run in filename order; the first
creates the `Falcon` database.

Apply them with the helper once the stack is up:

```bash
MSSQL_SA_PASSWORD=... ./scripts/init-db.sh            # from the host
# or inside the container:
docker compose exec -T sql-server /bin/bash < scripts/init-db.sh
```

The helper waits for SQL Server to accept connections, then applies every
`*.sql` file in `storage/sql/init`. Re-running is safe — each script is
idempotent (`CREATE DATABASE` is guarded by an existence check).

## Verifying connections

```bash
docker compose exec sql-server /opt/mssql-tools18/bin/sqlcmd \
  -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -No -Q "SELECT name FROM sys.databases"
```

`Falcon` should appear in the list after initialization.
