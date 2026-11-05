# MVP Round-Trip Message Contract (FALCON-031)

The exact message envelopes the go/no-go round trip uses, so the Commander
(Himel) and Follower (Maliha) build and validate against one contract. Every
message below is valid under [`src/schema/falcon-v1.json`](../src/schema/falcon-v1.json)
and is enforced by `tests/unit/test_mvp_roundtrip_contract.py`. Field meanings
are in [protocol.md](protocol.md) and [vocabulary.md](vocabulary.md).

## Validation on send and receive

Both agents call the shared guard from `src/schema/validator.py` on **send**
(before framing a message) and on **receive** (after decoding one):

```python
from src.schema.validator import validate, validate_or_raise

ok, errors = validate(message)        # returns (bool, [field: reason, ...])
validate_or_raise(message)            # returns the message, or raises
                                      # SchemaValidationError naming the field
```

If either side ever produces or accepts a message that fails this guard, the
round trip is not contract-compliant.

## 1. Command — `HOLD` (Commander → Follower)

```json
{
  "message_id": "<uuid>",
  "sender_id": "commander-1",
  "recipient_id": "follower-1",
  "timestamp": "<ISO 8601 UTC>",
  "schema_version": 1,
  "sender_role": "commander",
  "role_epoch": 0,
  "type": "command",
  "payload": { "command": "HOLD" }
}
```

`HOLD` carries no payload fields beyond `command`.

## 2. Response — `COMPLY` (Follower → Commander)

The healthy follower complies and clears movement. `in_reply_to` echoes the
command's `message_id`.

```json
{
  "message_id": "<uuid>",
  "sender_id": "follower-1",
  "recipient_id": "commander-1",
  "timestamp": "<ISO 8601 UTC>",
  "schema_version": 1,
  "sender_role": "follower",
  "role_epoch": 0,
  "type": "response",
  "payload": { "response": "COMPLY", "in_reply_to": "<command message_id>" }
}
```

## 3. Response — `DECLINE` / `BATTERY_CRITICAL` (decline path)

With the follower's battery forced to 5%, a movement-requiring command is
declined with the enumerated reason `BATTERY_CRITICAL`. For `HOLD` (which does
not require movement) the follower still complies; the decline path is exercised
by a movement command such as `PROCEED_TO_WAYPOINT` or `RETURN` under the forced
low battery. Decline leaves state unchanged.

```json
{
  "message_id": "<uuid>",
  "sender_id": "follower-1",
  "recipient_id": "commander-1",
  "timestamp": "<ISO 8601 UTC>",
  "schema_version": 1,
  "sender_role": "follower",
  "role_epoch": 0,
  "type": "response",
  "payload": {
    "response": "DECLINE",
    "in_reply_to": "<command message_id>",
    "reason": "BATTERY_CRITICAL"
  }
}
```

## Broadcast

A broadcast is the same `HOLD` command sent to each of the three followers
(`follower-1`, `follower-2`, `follower-3`) with `recipient_id` set per target.
Each follower returns its own `COMPLY` (or `DECLINE`) whose `in_reply_to`
matches the command it received, so responses are correlated by `message_id`.

## Contract ownership

- The envelopes, command/response vocabulary, and the `BATTERY_CRITICAL` reason
  are defined in the schema and docs (Wahid).
- Constructing and sending these messages, and running `decide()`, belong to the
  Commander and Follower agents (Himel, Maliha).
- Transport, Docker Compose, and exchange logging belong to the plumbing
  (Munem).
