# Protocol Error Behavior

What happens on every kind of bad input (FALCON-018). The rule: **every bad
input produces a documented error response, never a crash.** Protocol errors
are distinct from command declines — a `DECLINE` is a legitimate state-based
refusal of a *valid* message; an `ERROR` means the message itself was
unacceptable.

Errors are returned as a `response`-type message with `response: "ERROR"` and
an `error` object `{ code, message?, field?, agent? }`. Error codes are a fixed
enumeration in the schema. `error.agent` optionally names the agent an error is
about (for example, the unresponsive follower in a `NO_RESPONSE`).

| Case | `error.code` | Behavior |
| --- | --- | --- |
| Malformed JSON (unparseable bytes) | `MALFORMED_JSON` | Framing/parse layer rejects it; log the raw length and sender; do not attempt to act. |
| Unknown message `type` | `UNKNOWN_TYPE` | Reject; the `type` is not in the envelope enum. |
| Unsupported / unknown command name | `UNSUPPORTED_COMMAND` | Reject at validation before command policy runs. |
| Missing required field | `MISSING_FIELD` | Reject; `error.field` names the missing field. Also covers a wrong `schema_version`. |
| Invalid sender/recipient ID | `INVALID_SENDER_OR_RECIPIENT` | Reject; ID absent, malformed, or not a known peer. |
| Stale role epoch | `STALE_EPOCH` | Reject a message whose `role_epoch` is older than current. |
| Unauthorized request | `UNAUTHORIZED` | Reject; e.g. an agent attempting to self-promote out of turn, or a non-authorized role request. |
| Duplicate message ID | `DUPLICATE_MESSAGE` | Return the **cached** response for that `message_id`; do **not** re-execute (FALCON-072). |
| Follower did not answer | `NO_RESPONSE` | A commander waited the full response timeout for a follower and received nothing. The commander emits an `ERROR` with `error.agent` set to the silent follower's id, so the exchange is recorded and (for a middle agent) reported upward instead of being silently dropped. The follower's state is unknown, not changed. |

## Principles

- **Never crash.** Any malformed, oversized, truncated, or hostile input is
  caught and turned into one of the errors above (or safely dropped at the
  framing layer for unparseable bytes). Validated against fuzzing in Phase 10
  (FALCON-074).
- **Always identify the failure.** `error.field` / `error.message` carry enough
  context to debug (which field, which sender), and every error is written to
  the error/event log (FALCON-041).
- **Idempotent on duplicates.** Re-sending the same `message_id` returns the
  cached response, so retries after a lost reply are safe.
- **Stale authority is powerless.** The Follower rejects stale commands with
  `ERROR` and code `STALE_EPOCH` before invoking the command policy. The
  revived commander rejoins as a follower.
- **Silence is an event, not a gap.** Most error codes reject a *received*
  message; `NO_RESPONSE` is the exception — a commander *originates* it when a
  follower never answers, so a silent follower produces a recorded, reportable
  result rather than nothing. Adding `NO_RESPONSE` is a backward-compatible
  (MINOR) vocabulary addition under [versioning.md](versioning.md).

## Error vs decline — quick guide

| Situation | Result |
| --- | --- |
| Valid command, follower can't do it right now (battery, obstacle, busy, …) | `DECLINE` + decline reason |
| Message unparseable, wrong shape, unknown type/command, unauthorized, stale, duplicate | `ERROR` + error code |
