# Local Docker Setup

This guide explains how to bring the FALCON stack up locally. At this stage
every service is a placeholder skeleton — the goal is simply that a new
developer can start all four services with one command and see them running.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) 20.10+
- [Docker Compose](https://docs.docker.com/compose/) v2 (bundled with recent
  Docker Desktop; invoked as `docker compose`)
- Roughly 3 GB free disk for the SQL Server image

No cloud account, internet service, or external language model is required —
FALCON runs entirely offline.

## Services

| Service | Runtime | Purpose |
| --- | --- | --- |
| `commander` | Python | Issues commands to Followers (single instance) |
| `follower` | Python | Simulated agent; complies or declines (scaled later) |
| `storage-api` | .NET | Ingests JSONL logs and exposes a query API |
| `sql-server` | Microsoft SQL Server | Persistent storage for ingested exchanges |

The `commander` and `follower` services share one image (`falcon-agent`) and
differ only by the `FALCON_ROLE` environment variable.

## Bring the stack up

From the repository root:

```bash
docker compose up --build
```

This builds the agent and storage images, pulls the SQL Server image, and
starts all four services. The `commander`, `follower`, and `storage-api`
containers currently run idle placeholder loops and log a single startup line;
`sql-server` starts a real database engine.

Run it in the background instead with:

```bash
docker compose up --build -d
docker compose ps      # all four services should be listed as running
docker compose logs    # view startup output
```

## Configuration

The SQL Server `sa` password defaults to a local development value. To override
it, create a git-ignored `.env` file in the repository root:

```dotenv
MSSQL_SA_PASSWORD=Your_strong_Passw0rd
```

SQL Server requires a strong password (at least 8 characters including
uppercase, lowercase, a digit, and a symbol) or the container will refuse to
start. Never commit real credentials.

## Scaling Followers

Once the transport layer exists, multiple Followers can be started at once:

```bash
docker compose up --scale follower=10
```

## Bring the stack down

```bash
docker compose down          # stop and remove containers
docker compose down -v       # also remove the SQL Server data volume
```

## Troubleshooting

- **`sql-server` exits immediately** — the `sa` password does not meet
  complexity requirements, or the host has less than 2 GB RAM allocated to
  Docker. Check `docker compose logs sql-server`.
- **Port already in use** — another process is bound to `1433` (SQL Server) or
  `8080` (storage API). Stop it or change the host-side port mapping in
  `docker-compose.yml`.
- **Rebuild from scratch** — `docker compose build --no-cache` forces a clean
  image rebuild.
