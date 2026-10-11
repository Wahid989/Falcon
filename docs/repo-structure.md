# Repository Structure

The agreed folder layout (FALCON-004). Build your code in the right place so
the four workstreams stay separable.

```
src/
  agents/       Commander and Follower agent runtimes
  schema/       Message envelope, JSON Schema, and validator
  transport/    Transport interface and TCP implementation
  decision/     Capability-aware decision engine (pure functions)
  roles/        Role Manager, role state machine, epochs
  state/        Simulated follower state model and transitions
  logging/      JSONL exchange-log writer and rotation
  services/     Shared helpers / cross-cutting services

tests/
  unit/         Fast, isolated unit tests
  integration/  Cross-module and over-TCP integration tests
  e2e/          End-to-end acceptance scenarios
  scenarios/    YAML scenario files for the hierarchy test harness

storage/
  dotnet/       .NET log-ingestion service and query API (+ Dockerfile)
  sql/          SQL Server schema and init scripts (storage/sql/init)

scripts/        Developer/ops helper scripts (e.g. init-db.sh)
config/         Example/non-secret configuration (.env.example)
docs/           Project documentation
.github/        CI workflows, issue and PR templates
```

`src/` is a Python package tree (each subpackage has `__init__.py`), imported
as `src.<package>` — e.g. `from src.schema.validator import validate`. Naming
`src` the package root (rather than putting its subfolders directly on the path)
keeps `src/logging` from shadowing Python's standard-library `logging`.

## Note: `storage/` vs `database/`

The master plan's structure list (FALCON-004) names a top-level `database/`,
while FALCON-006, FALCON-007, and FALCON-042 all reference `storage/dotnet`.
To resolve the conflict we standardize on **`storage/`** as the single home for
persistence — `storage/dotnet` for the .NET service and `storage/sql` for SQL
scripts. There is no separate `database/` directory.
