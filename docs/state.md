# Simulated Follower State

The follower's world is **simulated** — no hardware, no sensors. This page
defines the state fields, their types, and defaults (FALCON-012). The typed
model is implemented in Phase 2 (FALCON-020) under `src/state/`; the decision
engine (Phase 4) reads this state to decide comply/decline.

## State fields

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `battery_percent` | int, 0–100 | `100` | Below 20 blocks movement commands (`BATTERY_CRITICAL`). |
| `obstacle_detected` | bool | `false` | Blocks `PROCEED_TO_WAYPOINT` (`OBSTACLE_DETECTED`). |
| `mechanical_fault` | bool | `false` | Blocks movement/health commands (`MECHANICAL_FAULT`). |
| `terrain` | string | `"flat"` | Current terrain; checked for suitability (`TERRAIN_UNSUITABLE`). |
| `capabilities` | list[string] | `[]` | Equipment/capabilities the agent carries; missing ones cause `CAPABILITY_UNAVAILABLE`. |
| `position` | string | `"base"` | Current waypoint/location name. |
| `movement_flag` | bool | `false` | Whether the agent is currently moving. |
| `current_command` | string \| null | `null` | The command in progress; non-null means `BUSY`. (Availability.) |
| `current_role` | enum | `"follower"` | `commander` or `follower`. |
| `role_epoch` | int, ≥ 0 | `0` | Authority version the agent currently recognizes. |
| `hierarchy_level` | int, ≥ 0 | `0` | Distance from the current top (0 = top). **Log-only** — see below. |
| `parent_id` | string \| null | `null` | The agent's commander (parent). `null` at the top. |
| `follower_ids` | list[string] | `[]` | The agent's direct followers. |

## `hierarchy_level` is log-only

`hierarchy_level` lives in the state object for computation and logging, but it
is **never** placed on the wire envelope or inside a status snapshot sent over
the network. It is recomputed as `parent level + 1` whenever the structure
changes (FALCON-040). The JSON Schema rejects it in any wire message.

## Default ("fresh") state

A newly started follower:

```json
{
  "battery_percent": 100,
  "obstacle_detected": false,
  "mechanical_fault": false,
  "terrain": "flat",
  "capabilities": [],
  "position": "base",
  "movement_flag": false,
  "current_command": null,
  "current_role": "follower",
  "role_epoch": 0,
  "parent_id": null,
  "follower_ids": []
}
```

## Forcing state for tests

The state model will expose `force_state(overrides)` and a YAML loader
(FALCON-020) so the scenario harness can drive any field to a specific value —
used only in tests/harness, never on the normal runtime path.
