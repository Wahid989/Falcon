# FALCON — Project Ground Rules for Claude

This file is read automatically at the start of every session. It records
project-wide decisions so they survive across sessions and teammates. Follow
it. When it conflicts with the original ticket plan, **this file and the
canonical plan (below) win.**

## Canonical plan

The authoritative, up-to-date ticket plan lives here (team-maintained):

- Google Doc: https://docs.google.com/document/d/1vLIpY3sGU9e5ZnlW-bgspzQAMpWzcmmXRMAkxdWVgiY/edit

If that doc and this file ever disagree, ask which is current before acting.

## What FALCON is

A bounded, **fully offline** command/response protocol — plus its reference
implementation — between one Commander agent and 10–20 simulated Follower
agents, with role switching, structured logging, and persistent storage.

- **Transport:** JSON over TCP for v1, kept behind an interface so it can be
  swapped later (ZeroMQ, MQTT) without touching the agents.
- **Vocabulary and decline reasons are a fixed enumeration.** No free text.
- **Follower state is simulated.** No hardware, no sensors.
- **Storage:** .NET services + Microsoft SQL Server; kept isolated from the
  Python agent runtime (agents write JSONL logs; the .NET service ingests them).
- No cloud, no internet, no external language model — ever.

## Decisions that override the original ticket plan

1. **Phase 11 (Packaging for SWIFT) is REMOVED.** Tickets 68–73 are not being
   done. FALCON is **not** packaged as a pip-installable library. Do not build
   toward `pip install falcon-protocol`, `setup.py`, or a versioned public API
   package.

2. **Ticket 04 uses `requirements.txt`, NOT `pyproject.toml`.** Since there is
   no library packaging, plain `requirements.txt` (optionally plus
   `requirements-dev.txt`) is the project config. Do not introduce a
   `pyproject.toml`. Pin: `pytest`, `jsonschema`, `pyyaml`, and core deps.

3. **SWIFT-handoff framing is trimmed** from the tickets that survive in other
   phases. Keep the docs themselves, drop the "for the SWIFT team" wording:
   - Ticket 12 — `docs/extending-the-vocabulary.md` (keep as a general
     extension guide).
   - Phase 6 intro / Ticket 38 — logging is for "testing and replay" (not
     "SWIFT handoff").
   - Ticket 55 — storage doc targets "a developer outside the storage owner."
   - Ticket 77 — "recommendations for future maintainers / the next FALCON
     iteration."

4. **Structural sequencing** with Phase 11 gone: Phase 10 (Hardening, 62–67)
   flows directly into Phase 12 (Report & Demo, 74–79). Ticket numbers are
   **not** renumbered — 68–73 are simply marked removed.

## Format usage (do not confuse these)

- **JSON** — the wire protocol (Commander↔Follower over TCP), the schema
  (`src/schema/falcon-v1.json`), the exchange logs (`*.jsonl`), stored payloads,
  and the .NET REST API responses. This is everything the running system
  produces or consumes.
- **YAML** — scenario files and runtime config (ports, IDs, forced state).
- **`requirements.txt`** — Python dependencies (see decision #2).
- **No TOML anywhere.**

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

## Working rules (from the plan)

- Keep the system **runnable after every work session**.
- Prefer small commits that reflect one completed unit of work (1–3/day when
  active on a ticket). Don't wait to finish a whole ticket before committing.
- Build order: **vocabulary and transport → agents → logging → role switching.**
- Keep transport logic behind an interface.
- Keep .NET storage isolated from the Python runtime (via JSONL logs, not
  direct calls).
- **Never** bake sponsor names, ticket numbers, or plan-phase names into
  runtime logs, error messages, or user-visible strings. (Comments and docs are
  fine.)

## Team ownership

| Owner | Area |
| --- | --- |
| Wahid Chowdhury | Vocabulary and schema (commands, decline enums, envelope, versioning, roles) |
| Munem Morshed | Transport and logging (TCP, framing, connection management, JSONL logs) |
| Maliha Chowdhury Adrita | Follower agent and scenario harness (decision function, simulated state, tests) |
| Himel Paul | Commander agent and integration (CLI, scripted mode, targeting, responses) |

## Status

Tickets 01–02 complete (repo structure, .gitignore, README, Docker Compose
skeleton, Dockerfiles, `docs/docker-setup.md`). Next up: Phase 0 remainder
(03 CI, 04 Python scaffolding with `requirements.txt`), then Phase 1 vocabulary.
