# FALCON Protocol — Message Envelope

Every FALCON message is a JSON object sharing one envelope. The authoritative
definition is the JSON Schema at [`src/schema/falcon-v1.json`](../src/schema/falcon-v1.json);
this page documents it (FALCON-014). Commands, responses, decline reasons, and
follower state are in [vocabulary.md](vocabulary.md) and [state.md](state.md).

## Envelope fields

| Field | Type | Valid values | Meaning |
| --- | --- | --- | --- |
| `message_id` | string | non-empty (UUID recommended) | Unique ID for this message; responses reference it via `in_reply_to`. |
| `sender_id` | string | `^[A-Za-z0-9_-]+$` | ID of the sending agent (e.g. `agent-01`). |
| `recipient_id` | string | `^[A-Za-z0-9_-]+$` | ID of the intended recipient. |
| `timestamp` | string | ISO 8601 UTC | When the message was created. |
| `schema_version` | integer | `1` | Protocol version; v1 messages MUST be `1`. |
| `sender_role` | string | `commander`, `follower` | The role the sender is acting as for this message. |
| `role_epoch` | integer | ≥ 0 | Authority version; incremented on each role handover. Used to reject stale authority. |
| `type` | string | `command`, `response`, `role_request`, `role_grant`, `role_deny`, `heartbeat` | Which kind of message this is; selects the `payload` shape. |
| `payload` | object | per `type` | Type-specific body (see below). |

`additionalProperties` is **false** — unknown envelope fields are rejected.

### `hierarchy_level` is NOT in the envelope

Each agent's distance from the current top (0 = top) is a **log-only** field
computed by the logging layer (FALCON-040). It never travels on the wire, and
the schema rejects it if present (including inside a status snapshot).

## Payload by message type

### `command`
```json
{ "command": "HOLD" }
{ "command": "PROCEED_TO_WAYPOINT", "waypoint": "north-fence" }
```
`command` is one of the four verbs. `waypoint` is **required** for
`PROCEED_TO_WAYPOINT` and **forbidden** for the others. See
[vocabulary.md](vocabulary.md#commands).

### `response`
```json
{ "response": "COMPLY", "in_reply_to": "<message_id>" }
{ "response": "DECLINE", "in_reply_to": "<id>", "reason": "BATTERY_CRITICAL" }
{ "response": "STATUS_RESPONSE", "in_reply_to": "<id>", "state": { ... } }
{ "response": "ERROR", "in_reply_to": "<id>", "error": { "code": "MISSING_FIELD" } }
```
`reason` is required for `DECLINE`, `state` for `STATUS_RESPONSE`, `error` for
`ERROR`.

### `role_request` / `role_grant` / `role_deny`
```json
{ "target_id": "agent-00", "requested_role": "commander" }
{ "in_reply_to": "<request id>", "granted": true, "new_epoch": 1 }
{ "in_reply_to": "<request id>", "reason": "not_authorized" }
```
A request may target any commander above the sender (skip-level). See
[vocabulary.md](vocabulary.md#role-management).

### `heartbeat`
```json
{ "status_summary": [ { "agent_id": "agent-02", "battery_percent": 80, ... } ] }
{ "status": { "battery_percent": 80, ... } }
```
The heartbeat is the positive **"I am alive"** signal. **Downward**
(commander → followers) carries every follower's status so siblings share one
view; **upward** (follower → commander) carries the sender's own status. Exactly
one of the two shapes is allowed. Heartbeats are kept **out** of the exchange
log (FALCON-039).

## Example (full envelope)

```json
{
  "message_id": "11111111-1111-1111-1111-111111111111",
  "sender_id": "agent-01",
  "recipient_id": "agent-02",
  "timestamp": "2026-01-01T00:00:00Z",
  "schema_version": 1,
  "sender_role": "commander",
  "role_epoch": 0,
  "type": "command",
  "payload": { "command": "PROCEED_TO_WAYPOINT", "waypoint": "north-fence" }
}
```
