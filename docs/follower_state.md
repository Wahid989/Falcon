# Follower Simulated State (FALCON-012)

All state is simulated. There is no hardware. Each field below is something the Follower checks before it answers a command with COMPLY or DECLINE.

## State fields

| Field | Type | Default | Linked decline reason |
|---|---|---|---|
| battery | integer, 0-100 | 100 | BATTERY_CRITICAL when below the configured threshold |
| terrain_capability | list of fixed terrain values | ["FLAT"] | TERRAIN_UNSUITABLE when the command needs terrain not in the list |
| obstacles | boolean | false | OBSTACLE_DETECTED when true |
| mechanical_condition | enum: OK or FAULT | OK | MECHANICAL_FAULT when FAULT |
| equipment | list of fixed equipment values | [] | CAPABILITY_UNAVAILABLE when a required item is missing |
| busy | boolean | false | BUSY when true |
| current_role | enum: FOLLOWER or COMMANDER | FOLLOWER | none |
| hierarchy_level | integer | 0 | none. LOG-ONLY, never sent on the wire. It equals parent level + 1. |
| parent | agent_id or null | null | none |
| followers | list of agent_id | [] | none |

## Decline reasons that do not depend on state

- INVALID_REQUEST: the message itself is bad (for example, a missing field).
- STALE_EPOCH: the message has an old role_epoch.

## Notes

- All values come from a fixed list. There is no free text.
- The exact terrain and equipment values are still to be agreed with Wahid.
- The battery threshold is configurable.
