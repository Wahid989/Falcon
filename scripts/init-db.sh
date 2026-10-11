#!/usr/bin/env bash
# Initialize the FALCON SQL Server database.
#
# Waits for the sql-server container to accept connections, then applies every
# SQL script in storage/sql/init in order. Idempotent — safe to re-run.
#
# Usage (from the repository root, with the stack up):
#   MSSQL_SA_PASSWORD=... ./scripts/init-db.sh [host] [port]
#
# Defaults: host=localhost port=1433. Requires sqlcmd on PATH, or run it inside
# the sql-server container:
#   docker compose exec -T sql-server /bin/bash < scripts/init-db.sh

set -euo pipefail

HOST="${1:-localhost}"
PORT="${2:-1433}"
PASSWORD="${MSSQL_SA_PASSWORD:?Set MSSQL_SA_PASSWORD}"
INIT_DIR="$(cd "$(dirname "$0")/.." && pwd)/storage/sql/init"

# Locate sqlcmd (path differs across image versions).
SQLCMD="$(command -v sqlcmd || true)"
for candidate in /opt/mssql-tools18/bin/sqlcmd /opt/mssql-tools/bin/sqlcmd; do
    [ -z "$SQLCMD" ] && [ -x "$candidate" ] && SQLCMD="$candidate"
done
[ -z "$SQLCMD" ] && { echo "sqlcmd not found on PATH" >&2; exit 1; }

echo "Waiting for SQL Server at ${HOST}:${PORT}..."
for i in $(seq 1 30); do
    if "$SQLCMD" -S "${HOST},${PORT}" -U sa -P "$PASSWORD" -No -Q "SELECT 1" >/dev/null 2>&1; then
        echo "SQL Server is ready."
        break
    fi
    [ "$i" -eq 30 ] && { echo "SQL Server did not become ready in time" >&2; exit 1; }
    sleep 2
done

for script in "$INIT_DIR"/*.sql; do
    echo "Applying $(basename "$script")..."
    "$SQLCMD" -S "${HOST},${PORT}" -U sa -P "$PASSWORD" -No -b -i "$script"
done

echo "Database initialization complete."
