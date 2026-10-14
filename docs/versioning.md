# Schema Versioning Strategy

How the FALCON protocol evolves without breaking existing agents (FALCON-017).
The current wire version is `schema_version = 1`
([`src/schema/falcon-v1.json`](../src/schema/falcon-v1.json)).

## Version numbering

FALCON uses a single integer `schema_version` on the envelope, backed by
semantic intent:

| Change | Bump | Rationale |
| --- | --- | --- |
| Add a new command | **MINOR** | Old agents ignore/decline it; existing messages still valid. |
| Add a new decline reason | **MINOR** | Additive; existing responses still valid. |
| Add `chain_id` (future group splitting) | **MINOR** | Backward-compatible; v1 defaults to a single root chain. |
| Change an existing command's payload | **MAJOR** | Breaks agents that expect the old shape. |
| Remove a command or field | **MAJOR** | Breaks agents that still send/expect it. |
| Rename a field or enum value | **MAJOR** | Equivalent to remove + add. |

The on-the-wire `schema_version` integer increments only on a **MAJOR** change
(v1 → v2). MINOR, backward-compatible additions stay within the same wire
version but are tracked in this document and the schema's change log.

## Version-mismatch handling

- A validator for version *N* **accepts** a message with `schema_version = N`.
- A message with a **different** `schema_version` is **rejected with a clear
  version-mismatch error**, never a crash. At the schema level, the v1 schema
  pins `schema_version` to the constant `1`, so a v0 or v2 message fails
  validation — which the agent surfaces as a protocol `ERROR`
  (`code: UNKNOWN_TYPE`/`MISSING_FIELD`-style mismatch; see
  [protocol-errors.md](protocol-errors.md)).
- A v2 validator, when introduced, must decide per message whether a v1 message
  is still acceptable (backward-compatible) or must be rejected with the
  version-mismatch error. The rule above (MINOR vs MAJOR) determines which.

A regression test asserts that a message with the wrong `schema_version` is
rejected by the v1 schema (see `tests/unit/test_schema_examples.py`,
`wrong_schema_version`).

## `chain_id` (reserved)

Dynamic group splitting is out of scope this term, but `chain_id` is reserved
as the future, backward-compatible (MINOR) way to distinguish independent
chains. v1 behaves as a single root chain and does not emit or require it.
