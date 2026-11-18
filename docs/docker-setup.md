# Local Docker setup

The default Compose project now runs the actual Commander and Follower over
TCP. Both write JSONL logs to `./logs`. Follow the complete
[MVP run guide](mvp-running.md) for the milestone gate, manual broadcast/decline,
run evidence, and configuration.

Requires Docker Desktop/Engine with Compose v2. Initial image builds download
Python 3.12 and pinned dependencies; the running agents use an internal network.

```bash
# One HOLD/COMPLY exchange; stop both when Commander exits.
docker compose up --build --abort-on-container-exit --exit-code-from commander
docker compose down

# All acceptance cases, fresh session, evidence saved, automatic cleanup.
.venv/bin/python -m scripts.run_mvp
```

The `broadcast` profile adds follower-2/3; `decline` adds follower-low at 5%.
These named services each have a unique identity/log file; blindly scaling a
single named follower would duplicate its identity and is not the MVP setup.
The one-shot Commander exits after its result; a persistent interactive mode
is documented in [transport.md](transport.md#actual-agent-sessions-falcon-028029).

SQL Server and the .NET API are optional and retain ports 1433/8080:

```bash
cp config/.env.example .env
docker compose --profile storage up -d --build sql-server storage-api
```

Configure local SQL credentials in the git-ignored .env. Storage details and
platform requirements are in [database.md](database.md). Agent acceptance does
not depend on SQL Server (particularly useful on ARM development machines).

Inspect service output with `docker compose logs`; rebuild changed source with
`docker compose build commander`. The full gate cleans up its unique project;
manual profile runs should use matching profiles when running `down`.

Storage services now use a separate normal `storage` bridge; only agents use
`falcon` with `internal: true`. Host ports 8080 (API/Swagger) and 1433 (SQL) stay
published. Recreate existing storage containers with
`docker compose --profile storage up -d --build --force-recreate sql-server storage-api`
to apply the network change. The shared log volume remains the only agent/storage
data path; agents do not join the storage network.
