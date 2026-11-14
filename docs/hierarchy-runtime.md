# Hierarchy runtime: chain model and dual-role agents

The code side of the [hierarchy data model](hierarchy-model.md):
- `src/agents/hierarchy.py` holds the chain itself;
- `src/agents/dual_role.py` lets one agent be a follower to its parent and a
  commander to its own followers at the same time.

## The chain (`Hierarchy`)

Each agent names its commander (its *parent*), and the top has none. The chain
is loaded from mission config at startup:

```yaml
# config/hierarchy/ground-drone.yml
role_epoch: 0
agents:
  - id: ground
  - id: drone-cmd
    parent: ground
  - id: drone-a
    parent: drone-cmd
```

```python
from src.agents.hierarchy import Hierarchy

chain = Hierarchy.load("config/hierarchy/ground-drone.yml")
chain.parent_of("drone-a")        # "drone-cmd"
chain.children_of("drone-cmd")    # ("drone-a", ...)
chain.level_of("drone-a")         # 2  (log-only, never on the wire)
```

| Call | Returns |
| --- | --- |
| `top`, `depth`, `agents` | The level-0 agent, the number of levels, all ids. |
| `parent_of(id)` / `children_of(id)` | The agent's commander (`None` at the top) and its direct followers. |
| `level_of(id)` / `levels()` | Distance from the top: `0` at the top, otherwise parent level + 1. |
| `path_to_top(id)` | The agent, its parent, and so on up to the top. |
| `subtree(id)` | The agent and everyone below it: the scope it commands. |
| `is_top` / `is_middle` / `is_leaf` | The agent's position. A middle agent has both a parent and followers. |
| `relation(id, other)` | `"parent"`, `"follower"` or `None`: how `other` relates to `id`. |
| `peer_levels(id)` | Levels of the agent's direct peers, for its exchange log. |
| `Hierarchy.chain(ids)` | A straight chain of any depth (each agent commands the next). |
| `to_config()` / `from_config()` / `load(path)` | Mission-config form. |

A `Hierarchy` never changes. Structure changes return a new, validated one:

| Change | Call |
| --- | --- |
| Move an agent, with its subtree, under a new commander | `with_parent(id, new_parent)` |
| Add an agent | `with_agent(id, parent)` |
| A granted role request, including a skip-level one | `with_new_top(id, role_epoch=n)` |

For `with_new_top`:
- the requester becomes the top and keeps its own followers;
- the previous top, with everything still under it, becomes the requester's
  follower, so its level goes from 0 to 1 and everything below it shifts down;
- the new epoch must be greater than the current one.

### Integrity

An invalid structure raises `HierarchyError`. Its `problems` list every rule
broken at once:
- no circular chains;
- no agent commanding itself;
- every commander exists;
- exactly one top;
- valid agent ids;
- `role_epoch` is a whole number ≥ 0.

`check_views(views)` compares what each agent believes (`AgentView.of(state)`
reads `parent_id`, `follower_ids`, `current_role` and `role_epoch` from its
state) with the chain. It reports:
- the wrong commander or followers;
- a follower claimed by two commanders (one commander per scope);
- an agent with no followers claiming to command;
- an epoch newer than the chain's.

`validate_views` raises instead of returning the list.

## Dual-role agents (`DualRoleAgent`)

```python
from src.agents.dual_role import DualRoleAgent, exchange_logger_for

agent = DualRoleAgent(
    "drone-cmd", chain,
    parent_link=link_to_ground,                         # None only for the top
    follower_links={"drone-a": link_a, "drone-b": link_b},  # one per follower
    exchange_log=exchange_logger_for(chain, "drone-cmd", "logs", session_id=run),
)
with agent:                                   # starts and stops routing
    agent.follower.handle_once(timeout=5)     # answers its commander
    agent.commander.issue("drone-a", "HOLD", timeout=5)   # commands its followers
```

The runtime owns the real links. The agent runs:
- a **Follower module** facing its parent (`agent.follower`, `None` at the
  top);
- a **Commander module** facing its followers (`agent.commander`).

Each module reads from its own view of a link, so both work at the same time.

### Routing

Every incoming message is routed by its sender, looked up in the hierarchy:

| Sender | Goes to |
| --- | --- |
| The agent's parent | Follower module |
| One of the agent's followers | Commander module |
| Anyone else, or a known peer on another peer's link | Rejected |

A rejected message gets `ERROR` with `INVALID_SENDER_OR_RECIPIENT` sent back
on the link it came from, unless it was itself a response (answering a
response could loop). It is kept in `agent.rejected` and passed to `on_reject`
if one is given. Neither module ever sees it.

The links must match the chain: a parent link exactly when the agent has a
commander, and one link per follower. Anything else raises `DualRoleError`.

`exchange_logger_for(chain, id, directory, session_id=...)` creates the
agent's exchange log with its own level and its peers' levels taken from the
chain. A middle agent at level 1 therefore logs:
- what it sends at level 1;
- what it receives from its commander at level 0;
- what it receives from its followers at level 2.

### When a link dies

Each link has its own reader, so a dead link only affects the module that
uses it.
- **Upstream link down:** `serve_once` raises `TransportClosedError`, while
  the Commander module keeps commanding the followers.
- **One follower's link down:** orders to that follower fail at once, with no
  hang, while the other followers and the commander above are unaffected.

`agent.link_is_down(peer)` tells the runtime which link died.
`agent.replace_link(peer, new_link)` swaps in a fresh link once that peer
reconnects. The module carries on over it, and the old link is retired: a
late message on it is ignored, and its closing doesn't affect the new one.
The exchange log stays whole, because every record is one complete line.

## Relaying commands down the chain

In the sponsor's case, Ground talks only to the Drone Commander, never to the
drones. Give a middle agent a `relay_timeout` and it passes the commands it
accepts down to its followers:

```python
middle = DualRoleAgent("drone-cmd", chain, parent_link=..., follower_links=...,
                       relay_timeout=1.0)
with middle:
    middle.serve_once(timeout=5)   # one command from Ground, relayed and answered
```

When a command arrives from above, the middle agent:
1. makes its own decision. If it declines, it answers `DECLINE` with its own
   reason, and nothing is relayed.
2. sends the same command, with the same waypoint, to each of its followers,
   and waits up to `relay_timeout` for all of them;
3. answers its commander with a summary:

| Followers' answers | Answer upward |
| --- | --- |
| All `COMPLY` | `COMPLY` |
| At least one `DECLINE` | `DECLINE` with the first decliner's reason (in follower-id order) |
| A follower timed out, sent an invalid or `ERROR` reply, or its link failed | **No answer**, so the commander's timeout reports `TIMEOUT` |

**Notes:**
- The protocol has no error code yet for "a follower didn't answer". Until it
  does, silence is passed up as silence, which is truthful and uses only valid
  messages. The rule lives in `summarize()` in `src/agents/relay.py`.
- Followers that complied stay complied, even when the summary is `DECLINE`.
  The vocabulary has no undo.
- `REPORT_STATUS` is never relayed. The middle agent answers with its own
  status.
- A relay that ended without a summary is kept in `middle.incomplete`, and
  `on_relay` is called with every relay's per-follower results.
- **Timeouts must shrink going down.** Each agent's relay timeout must be
  shorter than its commander's response timeout. Otherwise the commander gives
  up before the summary arrives.
- **Threading:** the relay uses the middle agent's Commander module from the
  thread that calls `serve_once`, so don't drive `middle.commander` directly
  at the same time.

This works at any depth: each middle agent relays to the level below it.

## Tests

- `tests/unit/test_relay.py` covers the summary rule.
- `tests/integration/test_ground_drone_scenario.py` runs the sponsor's chain
  from `config/hierarchy/ground-drone.yml`, over in-memory links and real TCP.
  It covers:
  - HOLD down and COMPLY up;
  - a drone's battery decline reaching Ground;
  - the Drone Commander declining itself;
  - waypoints;
  - status;
  - a silent drone;
  - every agent's log levels;
  - chains of depth 3 to 5.
- `tests/unit/test_hierarchy.py` covers:
  - mission config;
  - parents and children;
  - handover and skip-level updates;
  - traversal;
  - depths 1 to 50;
  - every integrity rule;
  - agent views.
- `tests/integration/test_dual_role_agent.py` covers:
  - a middle agent answering its commander while commanding its followers;
  - routing;
  - unknown and spoofed senders;
  - heartbeats;
  - log levels;
  - a chain of depth 2, 3, 5 and 8 where every level commands the next at
    once.
