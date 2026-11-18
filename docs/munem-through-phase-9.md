# Munem work batches through Phase 9

This audit uses SLIM_FALCON_Master_Plan_Sequential (001–095) and current repository
code. No revised attachment was visible with the instruction to continue phases;
new ownership/completion changes should be reconciled with this list.

## Phase 5 batch: 039–041

Branch: feat/falcon-039-041-phase-5-logging. Review: PR #24.

- 039: buffered append, flush/close, whole-record 100,000,000-byte rotation,
  ten archives, concurrency/rotation/shutdown tests, heartbeat exclusion.
- 040: live hierarchy metadata, skip-level 0 -> 1 shift, unknown sender flag.
- 041: persistent RoleManager outcomes plus protocol/connection/validation events.
- Local full suite: 624 passed in 16.59s; lint passed. Rebuilt Docker MVP GO.
- Remote CI passed for this batch. See docs/logging.md on that branch.

Shared 043 is not automatically closed by the Python/logger tests: coordinate
live SQL ingestion/constraint/concurrency/rollback verification with Himel.

## Phases 6 and 7

No additional exclusively Munem-owned tickets in the master plan. Shared role
switching and the remaining runtime/module/link reconfiguration are not declared
finished by these batches. Full eligibility checks (036) and end-to-end switching
need the corresponding owners' work; the MVP policy is deliberately narrower.

## Phase 8 batch: 061–062

Branch: feat/falcon-061-062-phase-8-liveness, independent of the Phase 5 branch.

The merged PR #22 supplies opt-in idle/status/deadline callbacks. This batch
finishes EOF fallback and in-flight hold safety, with real TCP silent-loss tests,
brief-hiccup/no-early-election checks, and identical sibling status views.
The full suite passed 617 tests in 17.30s; final focused concurrency/role checks
passed 8 tests. The counts differ from Phase 5 because these are independent
branches from main, rather than one silently merged working tree.

See docs/heartbeat.md. The monitor requires a RoleManager and explicit activation;
it is not silently enabled in the old MVP CLI. Existing succession logic remains
with RoleManager. Shared 060/063/064/065 and full fleet acceptance are not closed
by the heartbeat implementation alone. PR #23 subsequently merged successor relinking and module/routing
reconfiguration through connector/accept_link hooks. Production TCP wiring of
those hooks remains later runtime work (079/080).

## Phase 9 dependencies and owner handoff

The original master plan assigns **066–071 to Maliha**:

| Ticket | Prerequisite | Needed work |
| --- | --- | --- |
| 066 | 058/059 | Standalone depth-N harness that drives agents and YAML-forced state. |
| 067 | 066 | Handover, skip-level and promotion at depths 2/3/5/10. |
| 068 | 066 | Exactly-one-commander assertions across switches. |
| 069 | 066 | Delayed/stale message rejection at multiple depths. |
| 070 | 066 | Escalation, revival and log-level invariants at multiple depths. |
| 071 | 067–070 | Per-commit depth matrix under 60s, nightly depth-10/20-follower run, coverage artifact, required check. |

There is no standalone 066 harness or 067–070 matrix in the inspected checkout.
Existing selected-depth tests and the live dashboard are not substitutes for
those acceptance criteria. Do not publish a workflow claiming the full matrix
runs when its scenarios are absent. Munem can wire the real harness into CI once
its entry point, scenarios, reports and runtime budget are available, coordinating
with Maliha (071) and the later 088/089/091 CI tickets.

The team must review/merge batches; pushes do not mark GitHub tickets complete
or authorize merging without the required review. No work beyond Phase 9 is
included here: 072–095 remain outside this instruction's implementation scope.
