#!/usr/bin/env bash
# Run the SQL Server schema tests.
#
# Applies every init script twice (proving they are idempotent), then runs every
# test script in storage/sql/tests. Each test script rolls back its own data.
#
# Usage (from the repository root):
#   docker compose up -d sql-server
#   ./scripts/test-db.sh

set -euo pipefail

cd "$(dirname "$0")/.."

# Git Bash on Windows rewrites arguments that start with "/" into Windows paths,
# which breaks the container paths below.
export MSYS_NO_PATHCONV=1

# Default must match docker-compose.yml.
PASSWORD="${MSSQL_SA_PASSWORD:-Falcon_local_dev_1}"

run_sql() {
    docker compose exec -T sql-server /opt/mssql-tools18/bin/sqlcmd \
        -S localhost -U sa -P "$PASSWORD" -No -b "$@"
}

echo "Waiting for SQL Server..."
for i in $(seq 1 30); do
    run_sql -Q "SELECT 1" >/dev/null 2>&1 && break
    [ "$i" -eq 30 ] && { echo "SQL Server did not become ready in time" >&2; exit 1; }
    sleep 2
done

for pass in 1 2; do
    for script in storage/sql/init/*.sql; do
        echo "Applying $(basename "$script") (pass $pass)..."
        run_sql -i "/$script" >/dev/null
    done
done

for script in storage/sql/tests/*.sql; do
    echo "Running $(basename "$script")..."
    run_sql -i "/$script"
done
