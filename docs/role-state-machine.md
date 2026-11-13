# Role State Machine (FALCON-044)

The states an agent's authority passes through, and the legal transitions
between them. This is the contract the Role Manager and the role-switching
tickets (045–051) and failover tickets (060–065) implement against. Role
messages are defined in [vocabulary.md](vocabulary.md#role-management); the
`role_epoch` field is in [protocol.md](protocol.md).

## States

| State | Meaning |
| --- | --- |
| `FOLLOWER` | The agent obeys a commander. Default state for every agent at startup. |
| `COMMANDER` | The agent issues commands to its followers and is the authority for its scope. |
| `TRANSITIONING` | A handover is in progress: the agent has requested a role, or is being promoted, and the outcome is not yet settled. No commands are issued or obeyed as authority until it resolves. |

`current_role` in the follower state model holds `FOLLOWER` or `COMMANDER`
(see [state.md](state.md)); `TRANSITIONING` is a transient status during a
handover, not a resting value.

## Dual-role agents

Every agent carries both a Commander module and a Follower module. A **middle
agent** is a `FOLLOWER` upward (to its parent) and a `COMMANDER` downward (to its
own followers) **at the same time**. The state machine below describes a single
authority relationship; a middle agent runs one instance per direction. Only the
active module for a given direction processes messages.

## Legal transitions

```mermaid
stateDiagram-v2
    [*] --> FOLLOWER
    FOLLOWER --> TRANSITIONING: send REQUEST_ROLE / selected to self-promote
    TRANSITIONING --> COMMANDER: GRANT_ROLE received / self-promotion succeeds (role_epoch++)
    TRANSITIONING --> FOLLOWER: DENY_ROLE received / live re-check fails
    COMMANDER --> FOLLOWER: step down (revived or graceful handover; adopt newer role_epoch)
```

| From | To | Trigger | Epoch |
| --- | --- | --- | --- |
| `FOLLOWER` | `TRANSITIONING` | The agent sends `REQUEST_ROLE` (possibly skip-level), or the deterministic failover rule selects it to self-promote. | unchanged |
| `TRANSITIONING` | `COMMANDER` | A `GRANT_ROLE` is received, or self-promotion passes its live re-check. | **increments** |
| `TRANSITIONING` | `FOLLOWER` | A `DENY_ROLE` is received, or the self-promotion live re-check fails (role cascades to the next agent in ID order). | unchanged |
| `COMMANDER` | `FOLLOWER` | The agent steps down: a revived commander sees a higher `role_epoch`, or a graceful handover yields authority. It adopts the newer epoch. | adopts newer |

## Rules

- **No direct `FOLLOWER` → `COMMANDER`.** Every promotion passes through
  `TRANSITIONING`, so a half-finished handover is never mistaken for authority.
- **`role_epoch` increments only on a successful promotion** (grant or
  self-promotion) — never on a deny or a failed re-check.
- **A revived commander never reclaims the role automatically.** It rejoins as a
  `FOLLOWER`; the only way back is a later deliberate graceful handshake.
- **Stale or unauthorized role messages cause no transition** — they are
  rejected (see [protocol-errors.md](protocol-errors.md)) and logged.
- **Exactly one `COMMANDER` per scope.** Enforced during handover (FALCON-050).
