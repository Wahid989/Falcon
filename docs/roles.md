# Role Manager: role requests, grants and epochs

`src/roles/manager.py` implements dynamic role switching (FALCON-045 to 051)
against the [role state machine](role-state-machine.md) and the role messages
in [vocabulary.md](vocabulary.md#role-management). It has one `RoleManager`
per agent. The manager holds no transport: `request_role()` and `handle()`
return the messages to send.

```python
from src.roles.manager import RoleManager

roles = RoleManager("drone-a", chain, status=lambda: my_status_snapshot)
request = roles.request_role()          # role_request to the top (skip-level)
replies = top_roles.handle(request)     # on the top: [role_grant] or [role_deny]
roles.handle(replies[0])                # drone-a is now the top at epoch + 1
```

## What it tracks (045)

| | |
| --- | --- |
| `role` | `commander` (the top), `follower` (has a commander above), or `transitioning` (a request is waiting). A middle agent is `follower` here and still commands its own `followers()`. |
| `role_epoch` | The authority version this agent knows. |
| `hierarchy` | The agent's view of the chain (see [hierarchy-runtime.md](hierarchy-runtime.md)). |
| `get_current_commander()` | The agent's commander. `None` at the top, or while it doesn't know who commands. |
| `known_peers()` | Each direct peer and its role relative to this agent. |
| `peer_status` | The latest status heard from each peer (`observe_status`), used to check a requester's eligibility. |

## Requesting and granting (046 to 049)

- **A request may skip levels.** By default it goes straight to the top.
  `request_role(target)` refuses locally, sending nothing, when:
  - the target isn't a commander above this agent;
  - this agent is already the commander;
  - a request is already waiting;
  - this agent fails its own eligibility self-check.
- **Only the current top can grant**, because a grant makes the requester the
  new top. A request to an in-between commander is denied `not_authorized`.
- **What the grant checks:**
  - the request is addressed to this agent;
  - this agent is the top, at the request's epoch, with nothing pending;
  - the requester is a known agent and isn't this agent;
  - the requester's eligibility, from its last known status.
- **On a grant:**
  - the epoch goes up by one. That's the only time it changes, apart from the
    later failover self-promotion;
  - both managers apply the same change: the requester becomes the top and
    keeps its followers, and the previous top, with everything still under
    it, nests one level deeper;
  - both managers end up with the same chain and epoch.
- **A deny** carries exactly one reason from the schema: `busy`, `degraded`,
  `terrain_unsuitable`, `equipment_missing` or `not_authorized`. The epoch
  doesn't change.
- **Eligibility** is a pluggable check on a status snapshot. `basic_eligibility()`
  covers battery or fault (`degraded`), a current command (`busy`), terrain and
  equipment. It is a stand-in until the shared self-check (FALCON-036) exists.

## One commander, one level (050)

- **Concurrent requests.** Two requests at the same epoch: the first is
  granted and the second is now stale, so it gets `ERROR` / `STALE_EPOCH`.
  There are never two commanders.
- **Who updates.** Only the two agents directly involved update their view:
  the new top, and the old top, which now sits one level below it. Deeper
  agents aren't notified, and they learn the newer epoch from the next
  message that carries it.

## Stale authority and the revived commander (051)

`observe(message)` checks the epoch on every non-role message:
- **Older than ours:** the message is rejected as stale (returns `False`), and
  a `STALE_EPOCH_REJECTED` event is recorded.
- **Newer than ours:** the agent adopts it. If it thought it was the
  commander, it steps down to follower (a `stepped_down` record).
  - If the newer epoch came in a command or heartbeat sent to it, that sender
    is its commander now.
  - Otherwise it waits, with no known commander, until one commands it.
  - It never takes the role back on its own. The only way back is a new
    request.

## Failover

When a commander is lost, the same manager runs the deterministic failover
rule: `on_commander_lost()`, `candidate_timed_out()`, the live re-check and
self-promotion, vacancy, and refusing out-of-turn claims. See
[failover.md](failover.md).

## Records

Every grant, deny and step-down is recorded as a `role_change` record, and
every stale rejection as an `event` record. Both use the exchange-log format
the storage service ingests. They are in `roles.records` and passed to
`on_record`, for the runtime's log writer (FALCON-041) to append.

## Running agents

Give a `DualRoleAgent` a `roles=RoleManager(...)`:
- Role messages from its peers go to the manager, and the replies go back over
  the same link.
- Every other message's epoch is shown to the manager first, and the
  resulting epoch is stamped on the Commander module and the Follower state.
  So once a newer epoch is known, an old commander's orders are refused with
  `ERROR` / `STALE_EPOCH`.
- `agent.request_role()` sends a request over the link to the agent's
  commander.

## Not done yet

- **Skip-level over the network** needs a link from the requester to the
  top, which the runtime doesn't open yet (multi-agent runtime, FALCON-079 and
  080). The managers themselves handle skip-level fully.
- **Rewiring a running agent's modules after a grant** (the new top dropping
  its Follower module and commanding its former commander) also belongs to
  that runtime work. The managers already agree on the new chain.

## Tests

- `tests/unit/test_role_manager.py` (31):
  - tracking;
  - skip-level grant and nesting;
  - epoch only on grant;
  - every deny reason and eligibility check;
  - local refusals;
  - concurrent requests;
  - one-level propagation;
  - repeated and duplicate requests;
  - stale rejection and its record;
  - the revived commander, both knowing and not knowing its new commander;
  - late, forged and invalid answers;
  - the record format.
- `tests/integration/test_role_switching_agents.py` (4), with running agents:
  - a grant and a deny over links;
  - skip-level without a link;
  - a revived commander's order refused as stale, after which it steps down.
