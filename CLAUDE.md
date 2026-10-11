# FALCON — Project Ground Rules for Claude

This file is read automatically at the start of every session. It records
project-wide decisions so they survive across sessions and teammates. Follow
it. When it conflicts with an older ticket plan, **this file and the current
master plan (below) win.**

## Canonical plan

The authoritative plan is the sponsor-approved **SLIM FALCON Master Development
Plan** (tickets `FALCON-001`–`FALCON-095`, 15 phases, 10 milestones). It
supersedes all earlier "Ticket 01/02…" plans. A team-maintained copy also lives
in this Google Doc:

- https://docs.google.com/document/d/1vLIpY3sGU9e5ZnlW-bgspzQAMpWzcmmXRMAkxdWVgiY/edit

If the doc and this file ever disagree, ask which is current before acting.

## What FALCON is

A bounded, **fully offline** command/response protocol — plus its reference
implementation — between software agents arranged in a **dynamic command
hierarchy** (chain). Every agent carries both a Commander and a Follower module;
a middle agent is a follower upward and a commander downward.

## Confirmed design decisions (locked with the sponsor)

- **Transport:** JSON over TCP, behind a transport interface. ZeroMQ/MQTT are
  documented future scaling paths only — **not** development work this term.
- **Fixed enumeration** for vocabulary and decline reasons — no free text. The
  follower's world (battery, obstacles, faults, position, terrain, equipment) is
  **simulated** — no hardware.
- **Hierarchy is a chain** — one commander per level, arbitrary depth. Group
  splitting is **out of scope** (`chain_id` reserved for the future).
- **Role requests may skip levels** straight to the top; on grant, the requester
  becomes the new top and the previous hierarchy nests one level deeper.
- **Disconnect detection via idle heartbeat:** a commander sends "I am alive" +
  status to direct followers only when idle 5s; 3 missed intervals (15s)
  declares it gone. Both values configurable. Heartbeats are kept **out** of the
  exchange log.
- **Deterministic failover (no voting):** surviving agents pick the new
  commander by shared agent-ID ordering + an eligibility self-check; the chosen
  agent self-promotes, increments `role_epoch`, announces, re-checks at
  promotion, cascades if unfit; vacant-until-human if none fit.
- **Revived commander rejoins as a follower** — old-epoch commands are rejected
  as stale; it never reclaims the role automatically.
- **`hierarchy_level` is LOG-ONLY** (distance from the current top, 0 = top).
  **Never** on the wire envelope.
- **Storage:** .NET services + Microsoft SQL Server, kept isolated from the
  Python runtime (agents write JSONL logs; the .NET service ingests them).
- No cloud, no internet, no external language model — ever.

## Repo-specific decisions

1. **`storage/`, not `database/`.** The master plan's FALCON-004 structure list
   says `database/`, but FALCON-006/007/042 say `storage/dotnet`. We standardize
   on **`storage/`** (`storage/dotnet` + `storage/sql`). No separate `database/`.
   (See `docs/repo-structure.md`.)
2. **Python config is `requirements.txt` + `requirements-dev.txt`, not
   `pyproject.toml`.** There is no pip-packaging anywhere in the plan, so plain
   requirements files are the project config. **No TOML anywhere.** Pinned:
   jsonschema, pyyaml (runtime); pytest, black, flake8 (dev).
3. **These supersede the earlier (obsolete) plan's "Phase 11 removed / SWIFT"
   notes** — the master plan has no packaging phase and no "SWIFT"; the end state
   is a **sponsor handover** (FALCON-095, tag `v1.0.0`). Ignore any lingering
   SWIFT references.

## Format usage (do not confuse these)

- **JSON** — the wire protocol, the schema (`src/schema/falcon-v1.json`), the
  exchange logs (`*.jsonl`), stored payloads, and the .NET REST API. Everything
  the running system produces or consumes.
- **YAML** — scenario files (hierarchy test harness) and runtime config.
- **`requirements*.txt`** — Python dependencies. No TOML.

## Repository layout

```
src/{agents,schema,transport,decision,roles,state,logging,services}/
tests/{unit,integration,e2e,scenarios}/
storage/{dotnet,sql}/   scripts/   config/   docs/   .github/
```

`src/` is a Python package tree (import as `src.<pkg>`). Full detail in
`docs/repo-structure.md`.

## Working rules (from the plan)

- Keep the system **runnable after every work session**.
- Small commits, 1–3/day when active on a ticket; don't wait for a whole ticket.
- **Build the MVP early** (FALCON-031 round trip) before hierarchy/roles/
  failover. It is the go/no-go checkpoint (~week 9).
- Keep transport behind the interface; keep .NET storage isolated from the
  Python runtime (via JSONL logs, not direct calls).
- Docs and cleanup are **part of each ticket**, not separate phases (only the
  graded report/demo, Phase 15, are their own tickets).
- **Never** bake sponsor names, ticket numbers, or phase names into runtime
  logs, error messages, or user-visible strings. (Comments and docs are fine.)
- Branch off `main`, PR with one review, CI green before merge
  (`docs/git-workflow.md`).

## Out of scope this term

Physical drone/robot/sensors · real navigation/terrain sensing · autonomous
route generation · LLM integration · open-ended NL negotiation · group
splitting · production deployment · ZeroMQ · MQTT.

## Team ownership (workstreams)

| Owner | Workstream |
| --- | --- |
| Wahid Chowdhury | Protocol & schema (009-018, 044, 060-067 shared, 070) |
| Munem Morshed | Transport, logging & CI (005, 008, 025-030, 039-041, 061-062, 072-078, 088-091) |
| Maliha Chowdhury Adrita | Follower, decision & testing (012, 019-020, 032-037, 052, 065, 066-071, 083) |
| Himel Paul | Commander, hierarchy & storage (006-007, 021, 038/042-043, 054-059, 092) |

Shared (authority core): role switching 045-051, disconnect recovery
060/063/064, full integration + report/demo 093-095.

## Status

**Phase 0 (FALCON-001–008) complete:** repo + structure, Git workflow +
templates + labels, Python env (requirements + Makefile), CI (green), .NET
storage scaffold with `/health`, SQL Server dev setup + init strategy, Docker
Compose stack.

Manual GitHub-UI steps (cannot be done via the integration here): enable branch
protection on `main`, add the four members, create the Projects board and place
issues. See `docs/git-workflow.md`.

Next: **Phase 1 — Protocol definition (FALCON-009–018)**, Wahid leading.
