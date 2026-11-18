# Deterministic failover: what happens when a commander disappears

This is the sponsor-approved recovery rule (FALCON-060) and how it is
implemented (063, 064, and the authority checks in 073 and 075). There is no
voting. Every surviving agent applies the same rule to the same information,
so they all name the same new commander.

## 1. Detection (FALCON-061 and 062, transport side)

- A commander sends a heartbeat ("I am alive", plus status) to its direct
  followers only when the link has been idle for **5 s**. Any real message
  counts as alive.
- **3 missed intervals (15 s)** confirm that the commander is gone. Both
  values are configurable, and a single missed beat never triggers failover.
- The downward heartbeat carries **every** follower's status, so all siblings
  share one view of who is fit.
- On a confirmed loss, followers hold position, and the runtime calls
  `RoleManager.on_commander_lost()` on each survivor.

## 2. Selection

1. The candidates are the lost commander's **direct followers, sorted by
   agent id**.
2. The **first candidate that passes the eligibility self-check** is the
   successor. The check covers battery, fault, busy, terrain and equipment.
   A candidate whose status isn't known gets the benefit of the doubt, because
   its own live re-check (below) is the real gate.
3. If no direct follower fits, the search goes **one level deeper**: their
   followers, sorted by id.
4. If nobody fits, the role is **vacant until a human intervenes**. Nothing
   crashes; every survivor records `vacant`, and none has a commander.

Every survivor computes the successor from the statuses it last heard (feed
them with `observe_status`, the agent's own included). The same input means
the same answer.

## 3. Takeover

- **Self-promotion, with no grant.** The designated agent runs a **live
  re-check** on its real current state.
  - **If it passes:** the epoch goes up by one. The agent takes the lost
    commander's place: it **keeps its own followers**, gains the lost
    commander's other followers, and the lost commander stays in the chain as
    its follower. It then **announces** itself with a heartbeat at the new
    epoch to each of its followers, and reports upward to its new commander
    (the lost commander's commander) with a heartbeat carrying its status.
  - **If it fails** (its condition changed since the last heartbeat): it
    records `promotion_declined` with the reason and doesn't promote.
- **Cascade.** A survivor that hears no announcement from the expected
  successor in time calls `candidate_timed_out()` and expects the next
  candidate. A running agent does this itself: the expected successor gets
  the same window as a lost commander (interval x missed intervals).
- **Accepting.** A survivor accepts a newer-epoch heartbeat or command only
  from its own commander, or from the agent the rule designates, and then
  updates its view of the chain the same way the successor did. Anything else
  is an **out-of-turn claim**: it is refused, and an `UNAUTHORIZED_PROMOTION`
  event is recorded. A survivor that missed the loss still accepts the
  designated successor, because it can work out the same answer.
- **The commander above.** It accepts the successor's upward report and
  updates its chain the same way (`successor_accepted`). It **stays where it
  is**: a newer epoch reported from below means a failover happened further
  down, not that it was replaced. It accepts a successor from the lost
  agent's candidates (its followers, or one level deeper). It does not have
  their statuses, so it can't re-run the eligibility part of the rule; the
  survivors below are the ones that refuse an out-of-turn claim.
- **The epoch climbs.** Any agent that adopts a newer epoch from below
  reports it upward straight away, so the agents above don't send orders that
  would be refused as stale.

## 3b. Re-linking the running agents

The Role Manager changes the chain; the `DualRoleAgent` then follows it:
- It opens links to its new peers through the runtime's `connector`
  (a function from peer id to a fresh link). A peer it can't reach, such as
  the lost commander, is skipped.
- The peer at the other end takes the new link with `accept_link`. Messages
  on it are only accepted once the Role Manager accepts the sender (the
  designated successor, or a successor reporting upward).
- The Commander module drops followers it no longer has and adds new ones;
  the Follower module answers its new commander; a successor relays orders
  to its new followers (when a relay timeout is set); the exchange log
  stamps the new levels.
- The old link to a lost commander is never reused. If that agent comes
  back, it gets a fresh link (`accept_link` or `replace_link`), and only then
  rejoins as a follower.

Tests: `tests/integration/test_failover_relinking.py` runs Ground ->
Drone Commander -> three drones with heartbeats on, kills the Drone Commander,
and checks that drone-a takes over, the other drones and Ground accept it,
Ground stays the top, and Ground's next order goes through drone-a to the
drones. Also: an unfit first candidate skipped, a first candidate that dies
too (time-out to the next), and a failover in the middle of a 4-level chain.

## 4. Invalidation (FALCON-064)

- The new epoch makes the old commander's authority stale. Its orders, even
  delayed ones that were already in flight, are rejected as `STALE_EPOCH`
  (and the Follower module answers `ERROR` / `STALE_EPOCH`). This is recorded
  as a `STALE_EPOCH_REJECTED` event.
- **A revived commander** that hears the new commander steps down
  (`stepped_down`) and rejoins as a follower. It **never takes the role back
  on its own**; the only way back is a later role request.

## Records

Every outcome is written in the exchange-log format the storage service
ingests, as `role_change` records:
- `self_promoted`;
- `promotion_declined` (with a reason);
- `vacant`;
- `stepped_down`;
- `successor_accepted` (by the commander above).

Stale and out-of-turn rejections are `event` records.

## Code

| Piece | Where |
| --- | --- |
| Candidates and the chosen successor | `succession_candidates`, `choose_successor` in `src/roles/failover.py` |
| The chain after a takeover | `Hierarchy.after_failure` in `src/agents/hierarchy.py` |
| Loss, live re-check, promotion, cascade, vacancy, accepting or refusing | `on_commander_lost`, `candidate_timed_out`, `observe` on `RoleManager` (`src/roles/manager.py`) |

Tests: `tests/unit/test_failover.py` (22). They cover:
- the candidate order;
- every selection case;
- the takeover structure, including the deeper case;
- promotion and acceptance by every survivor;
- a declined live re-check with the cascade to the next candidate;
- the deeper search;
- all-unfit vacancy;
- a survivor that missed the loss;
- a revived commander, with its delayed old order refused;
- out-of-turn claims, during a failover and without one.
- the commander above accepting the successor (also one level deeper) and
  staying the top;
- a survivor timing out an expected successor that never announces.

## Not done here

- **Heartbeat activation** is opt-in on DualRoleAgent; see
  [heartbeat.md](heartbeat.md). It forwards statuses with observe_status and
  calls on_commander_lost after the configured missed intervals. The legacy
  one-shot/interactive MVP runners do not enable RoleManager monitoring.
- **A connector for TCP.** Re-linking works through the `connector` and
  `accept_link` hooks and is tested in memory. The TCP runtime still needs
  to supply them (079 and 080).
- **A revived commander rejoining over TCP** needs the same hooks.

The liveness runtime holds immediately on explicit parent EOF while retaining
its succession deadline. Brief reconnects do not elect a successor, and late
in-flight decisions cannot undo fallback. See [heartbeat.md](heartbeat.md).
