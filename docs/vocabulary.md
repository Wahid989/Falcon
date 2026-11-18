# FALCON Vocabulary

The fixed enumeration of commands, responses, decline reasons, and role
messages. No free text on either side. This page is authoritative alongside the
JSON Schema ([`src/schema/falcon-v1.json`](../src/schema/falcon-v1.json)); the
message envelope is in [protocol.md](protocol.md).

## Commands

Carried in a `command`-type message as `payload.command`.

| Command | Purpose | Payload | Expected follower behavior |
| --- | --- | --- | --- |
| `HOLD` | Stop and hold position. | none | Clear `movement_flag`; clear `current_command`. Replies `COMPLY`. |
| `PROCEED_TO_WAYPOINT` | Move to a named waypoint. | `waypoint` (string, **required**) | Set `position` → waypoint, `movement_flag` → true, `current_command` set. Replies `COMPLY`, or `DECLINE` if unable. |
| `RETURN` | Return to base. | none | Set `position` → `base`, `movement_flag` → true until arrival. Replies `COMPLY`. |
| `REPORT_STATUS` | Request a state snapshot. | none | No state change. Replies `STATUS_RESPONSE` with the current state. |

Only `PROCEED_TO_WAYPOINT` carries `waypoint`; it is forbidden on the others.

## Responses

Carried in a `response`-type message as `payload.response`, always with
`in_reply_to` set to the triggering `message_id`.

| Response | When | Extra payload |
| --- | --- | --- |
| `COMPLY` | The command was accepted and executed. | — |
| `DECLINE` | The command was refused for a state reason. | `reason` (decline enum, **required**) |
| `STATUS_RESPONSE` | Reply to `REPORT_STATUS`. | `state` (status snapshot, **required**) |
| `ERROR` | The message was invalid at the protocol level (not a state decision). | `error` (`{code, message?, field?}`, **required**) |

`DECLINE` vs `ERROR`: a `DECLINE` is a legitimate state-based refusal of a
valid command; an `ERROR` means the message itself was malformed or
unauthorized (see [protocol-errors.md](protocol-errors.md)).

## Decline reasons

Every reason maps to a specific, testable state condition. `state` fields are
defined in [state.md](state.md).

| Reason | Triggering condition |
| --- | --- |
| `BATTERY_CRITICAL` | `battery_percent` < 20 **and** the command requires movement (`PROCEED_TO_WAYPOINT`, `RETURN`). |
| `OBSTACLE_DETECTED` | `obstacle_detected` is true **and** the command is `PROCEED_TO_WAYPOINT`. |
| `MECHANICAL_FAULT` | `mechanical_fault` is true, for any command requiring movement/health. |
| `BUSY` | `current_command` is not null (already processing a command). |
| `INVALID_REQUEST` | Command not in the vocabulary, or a required payload field is missing/invalid at the decision layer. |
| `STALE_EPOCH` | Message `role_epoch` < the follower's current `role_epoch`. |
| `TERRAIN_UNSUITABLE` | The agent's current `terrain` is in its **configured unsuitable-terrain set** (exact string match), for a terrain-gated command — `PROCEED_TO_WAYPOINT` only (`RETURN` is the safe fallback and is not terrain-gated by default). Sponsor-added. |
| `CAPABILITY_UNAVAILABLE` | A command's **configured required-capability set is not a subset** of the agent's `capabilities` (exact string match; all required). Sponsor-added. |

**Configurable, not hardcoded.** Both triggers are driven by per-agent /
per-command configuration, so no terrain names or mandatory capabilities are
baked into the code. Defaults are **empty** (no terrain blocked, no capability
required), which keeps every other command answerable until the sponsor confirms
values. The decision layer and the role eligibility self-check (FALCON-036)
share one implementation of each check.

**Sponsor decisions (values, not mechanism):** the terrain value enumeration and
each command's / the commander role's required capabilities are a team/sponsor
decision, supplied as configuration — the contract here fixes only the rule.

The decision priority order (which reason wins when several apply) is defined
with the decision engine in Phase 4 (FALCON-037): command validity → battery →
terrain → capability → availability.

## Role management

Used for dynamic role switching (Phase 6). Three message types:

| Type | Payload | Meaning |
| --- | --- | --- |
| `role_request` | `target_id` (agent), `requested_role` = `commander` (optional) | Request to become commander. May target any commander above the sender (skip-level). |
| `role_grant` | `in_reply_to`, `granted` = true, `new_epoch` (int) | Grant; the requester becomes the new top and `role_epoch` increments to `new_epoch`. |
| `role_deny` | `in_reply_to`, `reason` | Refuse the request with one structured reason. |

Deny reasons (distinct from command decline reasons):

| Deny reason | Meaning |
| --- | --- |
| `busy` | The grantor/candidate is processing other work. |
| `degraded` | Battery/mechanical condition fails the eligibility self-check. |
| `terrain_unsuitable` | Terrain is unsuitable for the role. |
| `equipment_missing` | A required capability is absent. |
| `not_authorized` | The requester is not permitted to take the role. |

## Heartbeat

A `heartbeat`-type message is the positive **"I am alive"** signal, sent by a
commander to its direct followers only when the link has been idle (see the
failover design, Phase 8). It carries status, not commands:

- **Downward** (commander → followers): `status_summary` — an array of every
  follower's status, so siblings share one view without sibling links.
- **Upward** (follower → commander): `status` — the sender's own snapshot.

Heartbeats are excluded from the exchange log (FALCON-039).
