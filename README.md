# FALCON

> A bounded, fully offline command/response protocol between one Commander agent and 10–20 Follower agents, featuring role switching, structured logging, and persistent storage.

FALCON is built from scratch as a JSON-over-TCP protocol in which a single
Commander issues a fixed vocabulary of commands to a fleet of simulated
Followers, each of which either complies or declines with an enumerated reason.
The system runs entirely offline — no cloud, no internet, no external language
model.

## Project

- **Course:** ENEL 500 A/B Capstone — Software Engineering
- **Sponsor:** Tykans Group Inc.

## Team

| Name | Primary ownership |
| --- | --- |
| Wahid Chowdhury | Vocabulary and schema (commands, decline enums, envelope, versioning, roles) |
| Munem Morshed | Transport and logging (TCP, framing, connection management, JSONL logs) |
| Maliha Chowdhury Adrita | Follower agent and scenario harness (decision function, simulated state, tests) |
| Himel Paul | Commander agent and integration (CLI, scripted mode, targeting, responses) |

## Repository layout

```
src/
  agents/       Commander and Follower agent runtimes
  schema/       Message envelope, JSON Schema, and validator
  transport/    Transport interface and TCP implementation
  logging/      JSONL exchange-log writer and rotation
tests/          Unit, integration, and scenario tests
storage/
  dotnet/       .NET log-ingestion service and query API
  sql/          SQL Server schema and migrations
docs/           Vocabulary, transport, logging, storage, and setup docs
```

## Getting started

Local development runs through Docker Compose. See
[`docs/docker-setup.md`](docs/docker-setup.md) for how to bring the system up.

```bash
docker compose up
```

## Status

Early scaffolding. The build follows a phased plan: vocabulary and schema first,
then transport, then agents, then logging, role switching, and persistent
storage. The system is kept runnable after every work session.
