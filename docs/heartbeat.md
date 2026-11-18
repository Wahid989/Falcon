# Heartbeat and failover handoff

The heartbeat integration is opt-in on `DualRoleAgent`, which is the runtime
that already owns a RoleManager. It does not silently enable elections in the
older one-shot/interactive MVP runners (those have no RoleManager).

```python
agent = DualRoleAgent(
    agent_id,
    hierarchy,
    parent_link=parent_link,
    follower_links=follower_links,
    roles=role_manager,
    heartbeat_interval=5.0,
    heartbeat_misses=3,
    # Required at the top, which has no Follower module to provide live state.
    heartbeat_status=live_wire_status,
)
agent.start()
# agent.stop() stops both readers and the heartbeat worker.
```

For a leaf/middle agent, heartbeat_status defaults to the real Follower's
status_snapshot. A top agent must supply a live status callable (battery and
other actual simulated fields), rather than a fabricated healthy status.
Heartbeat interval must be positive/finite; missed intervals a positive integer.
Leaving heartbeat_interval unset preserves existing behavior.

## Traffic and callbacks

- After a direct link has no normal traffic for 5 seconds, a commander sends
  status_summary downward. Each summary includes all **known** direct-follower
  statuses. Unknown health is never invented. Followers send their own status
  upward on the same idle cadence, so the commander can refresh that summary.
- A received heartbeat's schema, source link, sender, recipient, sender role,
  shape/direction and epoch are checked before it can refresh the timer or feed
  status data. Wrong/stale/forged traffic does not keep a commander alive.
- Every known snapshot in the parent's summary is passed to
  `RoleManager.observe_status(agent_id, snapshot)`. An upward heartbeat updates
  only its actual sender's status. Copies prevent later message mutations from
  changing the RoleManager cache. Local status is refreshed before selection.
- Valid normal messages from the parent also reset the loss timer. Our outgoing
  messages do not prove the parent is alive. Opposing heartbeats do not suppress
  each other's cadence, avoiding an upward/downward starvation loop.
- At 3 missed intervals (15 seconds by default), the follower clears movement
  and current_command, refreshes its local status, then calls
  `RoleManager.on_commander_lost(parent_id)` **once for that loss**. A single
  missed interval or socket EOF alone is not grounds for promotion.
- Failover announcements returned by RoleManager go through `_send_all` on
  existing links, with module epochs synchronized. Election, eligibility,
  promotion, epoch increment, and vacancy remain the existing RoleManager's job.
- All RoleManager callbacks run under the same runtime lock as role routing.
  Heartbeats are consumed by the runtime, not queued as commands/responses,
  and remain excluded from exchange JSONL logs.

## Scope limits

This wires the requested status/loss handoff and default idle/deadline behavior;
it is not a complete reconfiguration of post-failover peer links. Announcements
for missing/failed links remain visible in `DualRoleAgent.undeliverable`.
Successor-to-sibling link establishment, updated command routing/module ownership,
and successor-announcement cascade timing remain the runtime work described in
[failover.md](failover.md) and later integration tickets. Unknown peer status
continues to use the RoleManager's existing selection/live-recheck policy.
Do not describe enabling this monitor alone as a full fleet failover demo.

## Verification

```bash
.venv/bin/python -m pytest tests/unit/test_heartbeat.py tests/integration/test_heartbeat_failover.py -v
```

Fake-clock tests verify exact deadlines without 15-second sleeps: status forwarding,
no failover at one/two missed intervals, normal traffic refreshing the timer,
rejection of stale/spoofed/invalid beats, idle sending and no starvation. A live
runtime test verifies upward status delivery, continued liveness, and exactly one
call to the real failover implementation after the parent stops, including hold
and epoch propagation. The real failover selection tests remain unchanged.

## Windows transport fixes

The listener uses SO_EXCLUSIVEADDRUSE on Windows; SO_REUSEADDR there can allow
multiple listeners to claim the same address. POSIX retains SO_REUSEADDR so normal
restart behavior is preserved. A send that repeatedly reports BlockingIOError
still checks its deadline even if select continuously reports writable.

The backpressure regression writes a real partial frame and deterministically
blocks subsequent sends. It no longer assumes an OS socket buffer size proves
backpressure. Graceful idle shutdown uses SIGTERM on POSIX or CTRL_BREAK_EVENT
in a new Windows process group; the runner handles SIGBREAK when available.
Forced Windows terminate() is still available for crash/cleanup tests, but cannot
claim to exercise graceful shutdown. A Windows CI job runs these regressions;
macOS/Linux results alone do not certify a Windows run.

## Complete liveness/fallback behavior (061/062)

A confirmed parent EOF now enters safe hold immediately; it does **not** elect a
successor immediately or reset/shorten the original missed-heartbeat deadline.
If a fresh link delivers valid parent traffic before the deadline, liveness is
restored and no election is triggered. Movement is not automatically resumed;
a new valid command is required. A child's disconnect never declares this
agent's parent dead.

Hold captures the simulated position, clears movement/current_command, and
remains enforced when an already-running decision completes late. A reply that
fails after disconnect cannot undo the hold or move the simulated agent to a
late waypoint. On confirmed silent loss the existing RoleManager callback still
runs exactly once, and local state is refreshed before eligibility selection.

Both silent loss on an open TCP socket and explicit EOF are tested with the
real dual-role runtime. Fake clocks verify the default 5-second/three-interval
boundary, normal traffic renewal, brief hiccups and consistent sibling status
views. These are opt-in runtime facilities; activating them in a deployment
requires a RoleManager and a live status provider at the top as shown above.

This completes Munem's liveness/fallback implementation in its documented runtime
scope. It does not certify the entire Phase 8 milestone: full eligibility policy,
successor-announcement cascade timing, post-promotion link/module reconfiguration,
and the full fleet failover harness remain coordinated/shared work. In particular,
changing the current top must not be advertised as a working end-to-end fleet
handover until those missing runtime paths are covered.
