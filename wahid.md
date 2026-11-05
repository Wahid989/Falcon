# FALCON — Wahid Chowdhury · Ownership & Delivery

**Workstream:** Protocol & schema
**Guiding question:** *What does a FALCON message mean, and who may hold authority?*

This document records the tickets I own in the SLIM FALCON master plan, what has
been delivered, and what remains. It is maintained as my workstream progresses.

**Status legend:** ✅ Done (merged to `main`) · 🟡 In progress · ⬜ Not started

---

## 1. Scope of ownership

Per the master plan's four-person work split, I own the protocol contract — the
vocabulary, message envelope, JSON Schema, versioning, and error behavior — plus
the validator, the authority/role vocabulary, and the hierarchy data model. I
co-own the authority core (role switching and deterministic failover) with Himel
and Maliha, and contribute the protocol sections of the all-hands integration,
report, and demo.

| Area | Tickets | Type |
| --- | --- | --- |
| Vocabulary, envelope, schema, versioning, errors | 009–011, 013–018 | Sole owner |
| Protocol models & validator | 019 | Sole owner |
| Role state machine | 044 | Sole owner |
| Hierarchy data model | 053 | Sole owner |
| Scope boundary (group splitting out of scope) | 082 | Sole owner |
| Role switching / Role Manager (authority core) | 045–051 | Shared (Wahid + Himel + Maliha) |
| Deterministic failover rule & invalidation | 060, 063, 064 | Shared (Wahid + Himel + Maliha) |
| Delayed / unauthorized message handling | 073, 075 | Shared |
| MVP checkpoint — message contract slice | 031 | All hands (my slice) |
| Full integration & report/demo | 083–087, 093–095 | All hands |

> Note: FALCON-012 (*Define simulated follower state*) is owned by Maliha. I
> authored `docs/state.md` as supporting contract documentation, but the ticket
> and its implementation (`src/state/`) are hers.

---

## 2. Completed & merged ✅

All Phase 1 protocol tickets, the validator (019), and my slice of the MVP
checkpoint (031) are merged to `main`.

| Ticket | Title | Deliverable | Acceptance met |
| --- | --- | --- | --- |
| 009 | Command vocabulary | `docs/vocabulary.md` — HOLD, PROCEED_TO_WAYPOINT, RETURN, REPORT_STATUS with payloads and follower behavior | Every command has a documented payload shape |
| 010 | Response vocabulary | `docs/vocabulary.md` — COMPLY, DECLINE, ERROR, STATUS_RESPONSE with payloads | Each response type documented |
| 011 | Decline reason enum | `docs/vocabulary.md` — 8 reasons, each mapped to a testable state condition (incl. sponsor-added TERRAIN_UNSUITABLE, CAPABILITY_UNAVAILABLE) | Every reason has a documented, testable trigger |
| 013 | Acceptance criteria per command | `docs/acceptance-criteria.md` — Given/When/Then across relevant states | Every command × relevant state documented |
| 014 | Base message envelope | `docs/protocol.md` — 9-field envelope; `hierarchy_level` excluded from the wire | Every field documented; `hierarchy_level` off the wire |
| 015 | Command & response schema | `src/schema/falcon-v1.json` + `tests/unit/test_schema_examples.py` | Valid messages pass; invalid fail |
| 016 | Role-management & heartbeat schema | `src/schema/falcon-v1.json` — REQUEST/GRANT/DENY role + heartbeat (down = all statuses, up = own) | Role and heartbeat messages validate |
| 017 | Schema versioning strategy | `docs/versioning.md` — MINOR/MAJOR rules, reserved `chain_id`, version-mismatch | Rules documented; mismatch never crashes |
| 018 | Protocol error behavior | `docs/protocol-errors.md` — every bad input → documented ERROR code | Each case produces an error, never a crash |
| 019 | Protocol models & validator | `src/schema/validator.py` (`validate`, `is_valid`, loaded once at import) + `tests/unit/test_validator.py` | Good pass / bad fail with field named; 32 tests (≥20) green in CI |
| 031 | MVP checkpoint — message contract (my slice) | `docs/mvp-roundtrip.md`; `validate_or_raise` + `SchemaValidationError` in `src/schema/validator.py`; `tests/unit/test_mvp_roundtrip_contract.py` | HOLD/COMPLY/DECLINE envelopes defined; `BATTERY_CRITICAL` enumerated; send/receive validation available and exercised |

**Merged via:** Phase 0–1 protocol work (direct to `main`), PR #7 (validator),
PR #9 (MVP message contract).

---

## 3. Key protocol decisions I authored

These are design choices the schema and docs now encode; they are binding on the
agents that validate against the contract.

- **Command/response name lives in `payload`, not the envelope.** The envelope
  carries `type`; `payload.command` / `payload.response` carry the specific name
  (per the master plan's envelope field list).
- **`hierarchy_level` is log-only.** It never appears on the wire envelope or in
  a status snapshot; the schema rejects it there.
- **Decline priority order:** command validity → stale epoch → battery → terrain
  → capability → availability (formalized with the decision engine in Phase 4).
- **Movement gating:** `BATTERY_CRITICAL` / `MECHANICAL_FAULT` gate movement
  commands (PROCEED_TO_WAYPOINT, RETURN); `OBSTACLE_DETECTED` gates only
  PROCEED_TO_WAYPOINT; RETURN is allowed past an obstacle as the safe fallback.
- **Versioning:** additive changes (new command / reason / `chain_id`) are
  MINOR; payload changes and removals are MAJOR; a wrong `schema_version` is
  rejected as a version mismatch, never a crash.
- **Validation guard:** `validate_or_raise()` is the single send/receive guard
  both agents call, so every message in an exchange is checked against the v1
  contract.

---

## 4. Shared responsibilities (authority core)

Co-owned with Himel and Maliha; my lane is the protocol/vocabulary side.

| Ticket | Title | My contribution |
| --- | --- | --- |
| 044 ⬜ | Role state machine | Define FOLLOWER / COMMANDER / TRANSITIONING states and legal transitions |
| 045–051 ⬜ | Role Manager, epoch, REQUEST/GRANT/DENY, exactly-one-commander, stale/revived | Role message contracts and epoch rules |
| 060 ⬜ | Deterministic failover rule | Document the sponsor-approved escalation contract |
| 063, 064 ⬜ | Self-promotion & stale-authority invalidation | Epoch/stale-command semantics |
| 073, 075 ⬜ | Delayed / unauthorized message handling | Epoch-based rejection, authority checks |

---

## 5. Upcoming owned tickets

| Ticket | Title | Status |
| --- | --- | --- |
| 044 | Role state machine | ⬜ Not started (Phase 6) |
| 053 | Hierarchy data model | ⬜ Not started (Phase 7) |
| 082 | Confirm group splitting out of scope | ⬜ Not started (Phase 11) |
| 093 | Technical report — protocol specification section | ⬜ Not started (Phase 15) |

My report section (093) must match `src/schema/falcon-v1.json` exactly and cover
the trade studies I own: bounded vocabulary vs. LLM interpretation, JSON vs.
Protocol Buffers, and log-only `hierarchy_level`.

---

## 6. File index (my contract on disk)

| File | Purpose |
| --- | --- |
| `src/schema/falcon-v1.json` | The authoritative v1 message schema |
| `src/schema/validator.py` | `validate`, `is_valid`, `validate_or_raise`, `SchemaValidationError` |
| `docs/protocol.md` | Message envelope |
| `docs/vocabulary.md` | Commands, responses, decline reasons, role/heartbeat |
| `docs/state.md` | Follower state fields (supporting 012; owned by Maliha) |
| `docs/acceptance-criteria.md` | Given/When/Then per command |
| `docs/versioning.md` | Schema versioning strategy |
| `docs/protocol-errors.md` | Protocol error behavior |
| `docs/mvp-roundtrip.md` | FALCON-031 round-trip message contract |
| `tests/unit/test_schema_examples.py` | Schema validity cases |
| `tests/unit/test_validator.py` | Validator unit tests |
| `tests/unit/test_mvp_roundtrip_contract.py` | MVP round-trip contract tests |
