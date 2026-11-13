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

## Tests

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
