# Acceptance Criteria per Command

Given/When/Then for every command across the relevant states (FALCON-013).
These drive the decision-engine tests (Phase 4, FALCON-037) and the scenario
harness (Phase 9). State fields are defined in [state.md](state.md); reasons in
[vocabulary.md](vocabulary.md).

Notation: **Given** a follower state, **When** it receives a command, **Then**
it returns a response and its state becomes as stated.

## Selected implementation policy

Command validity → battery → mechanical fault → obstacle → terrain →
capability → busy → COMPLY. The first applicable guard determines the
decline reason. The plan left the placement of the mechanical fault and
obstacle guards unspecified. Their placement here is flagged for team review.

The Follower validates commands and checks sender authority and epoch before
invoking the policy. FALCON-033 implements the battery, mechanical fault,
obstacle, and busy guards. Terrain and capability retain their positions in
the selected order for FALCON-034 and FALCON-035.

## HOLD

| Given | When | Then response | Then state |
| --- | --- | --- | --- |
| Healthy, idle | `HOLD` | `COMPLY` | `movement_flag` = false, `current_command` = null |
| Moving with no active command | `HOLD` | `COMPLY` | `movement_flag` = false, `current_command` = null |
| `current_command` set (busy) | `HOLD` | `DECLINE` `BUSY` | unchanged |
| `role_epoch` of message < current | `HOLD` | `DECLINE` `STALE_EPOCH` | unchanged |

(`HOLD` is a stop command: it does not require movement, so battery/terrain do
not block it.)

## PROCEED_TO_WAYPOINT (requires `waypoint`)

| Given | When | Then response | Then state |
| --- | --- | --- | --- |
| Healthy, idle, battery ≥ 20, no obstacle, terrain suitable, capability present | `PROCEED_TO_WAYPOINT north-fence` | `COMPLY` | `position` = `north-fence`, `movement_flag` = true, `current_command` set |
| `battery_percent` < 20 | same | `DECLINE` `BATTERY_CRITICAL` | unchanged |
| `obstacle_detected` = true | same | `DECLINE` `OBSTACLE_DETECTED` | unchanged |
| `mechanical_fault` = true | same | `DECLINE` `MECHANICAL_FAULT` | unchanged |
| terrain unsuitable | same | `DECLINE` `TERRAIN_UNSUITABLE` | unchanged |
| required capability missing | same | `DECLINE` `CAPABILITY_UNAVAILABLE` | unchanged |
| `current_command` set (busy) | same | `DECLINE` `BUSY` | unchanged |
| payload missing `waypoint` | same | `DECLINE` `INVALID_REQUEST` (or protocol `ERROR` if caught at validation) | unchanged |

## RETURN

| Given | When | Then response | Then state |
| --- | --- | --- | --- |
| Healthy, idle, battery ≥ 20 | `RETURN` | `COMPLY` | `position` = `base`, `movement_flag` = true until arrival |
| `battery_percent` < 20 | `RETURN` | `DECLINE` `BATTERY_CRITICAL` | unchanged |
| `mechanical_fault` = true | `RETURN` | `DECLINE` `MECHANICAL_FAULT` | unchanged |
| `current_command` set (busy) | `RETURN` | `DECLINE` `BUSY` | unchanged |

(`RETURN` requires movement, so battery and mechanical fault block it; an
obstacle does not, since returning to base is the safe fallback.)

## REPORT_STATUS

| Given | When | Then response | Then state |
| --- | --- | --- | --- |
| Any state | `REPORT_STATUS` | `STATUS_RESPONSE` with the current `state` snapshot | unchanged |
| `role_epoch` of message < current | `REPORT_STATUS` | `DECLINE` `STALE_EPOCH` | unchanged |

(`REPORT_STATUS` is always answerable from any healthy or degraded state; it
never changes state.)

## Cross-cutting rules

- A message whose `role_epoch` is **older** than the follower's current epoch is
  declined `STALE_EPOCH` regardless of command.
- A malformed or unauthorized message produces a protocol `ERROR`, not a
  `DECLINE` — see [protocol-errors.md](protocol-errors.md).
- Comply state changes are deterministic: the same (command, state) always
  yields the same response and next state.
