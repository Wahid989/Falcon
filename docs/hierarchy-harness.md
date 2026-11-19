# Parameterized hierarchy acceptance harness

Run the Phase 9 suite from the repository root with the Python requirements
installed.

```sh
python -m scripts.hierarchy_harness
python -m scripts.hierarchy_harness --depth 5 --scenario tests/scenarios/hierarchy.yml
python -m scripts.hierarchy_harness --depth 10 --followers 20
```

`--depth` accepts one or more counts including the top. The default matrix is
2, 3, 5 and 10. `--followers` sets the total width at the last commander in
the chain, including the chain's last agent. The remaining followers are
named `standby-00`, `standby-01`, and so on.

The harness creates agents named `agent-00` (top), `agent-01`, and so on.
The scenario's `states` mapping seeds each follower's `FollowerState`. A
configuration for a follower outside the generated topology applies only
when that follower is present. The default scenario reads back a configured
battery and position through a real `REPORT_STATUS` exchange.

Each depth runs direct handover, skip-level handover, confirmed disconnect
and self-promotion, and rejoining over a fresh link. Depth 2 exercises a
request to the top with no intermediate level. The harness checks for
overlapping authority during message delivery, verifies one commander per
scope after the change, correlates stale-order errors with their original
orders, and reads the former top's post-handover hierarchy level from JSONL.

The harness drives real agent modules over in-memory links with deterministic
delivery. It supplies confirmed loss at the runtime boundary. Timeout
detection is covered by the dedicated heartbeat integration tests.

GitHub Actions runs the matrix on pushes and pull requests and runs the depth
10, 20-follower scenario nightly. Each run has a 60-second execution budget
and publishes the `hierarchy-coverage` XML artifact. After the first GitHub
run, a repository admin can require the **Hierarchy depth matrix** status
check in the `main` branch ruleset to make failures block merges.
