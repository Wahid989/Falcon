# Commander module

The Commander is the command-issuing side of an agent
(`src/agents/commander.py`). It:
- builds FALCON v1 command envelopes;
- validates each one before it leaves;
- sends it over the transport;
- matches every follower response to the command it answers.

Every agent carries one. A middle agent uses it to face its own followers.

## Usage

```python
from src.agents.commander import Commander, Outcome
from src.transport import InMemoryTransport

commander_end, follower_end = InMemoryTransport.pair()
commander_end.connect()
follower_end.connect()

commander = Commander("ground", role_epoch=0)   # uses src.schema.validator
commander.add_follower("drone-cmd", commander_end)

result = commander.issue("drone-cmd", "HOLD", timeout=2.0)
if result.outcome is Outcome.DECLINE:
    print(result.reason)                         # e.g. BATTERY_CRITICAL
```

The runtime creates the links and owns their lifetime, as described in
[transport.md](transport.md). Register one link per direct follower with
`add_follower`.

## API

| Call | What it does |
| --- | --- |
| `Commander(agent_id, *, role_epoch=0, validator=None, clock=..., new_id=...)` | `validator` is a `validate(message) -> (ok, errors)` callable. It defaults to `src.schema.validator.validate`. `clock` and `new_id` can be swapped out for tests. |
| `add_follower(id, transport)` / `remove_follower(id)` / `followers` | Manage the links to direct followers. |
| `build_command(recipient_id, command, *, waypoint=None)` | Returns a validated envelope. Raises `InvalidCommandError` (with `.errors`) if the command is invalid. |
| `send_command(follower_id, command, *, waypoint=None)` | Builds, validates and sends a command. Returns the message sent. |
| `await_response(command, timeout)` | Waits up to `timeout` seconds and returns a `CommandResult`. |
| `issue(follower_id, command, *, waypoint=None, timeout)` | `send_command` followed by `await_response`. |
| `broadcast(command, *, waypoint=None)` | Sends the command to every direct follower, one message each. Returns `BroadcastResult(sent, failed)`. |
| `other_messages(follower_id)` | Removes and returns messages no command claimed: heartbeats, and replies that arrived after their command timed out. |
| `rejected_responses(follower_id)` | Responses that failed validation, with the reasons. |

`role_epoch` is a plain attribute. Role management (FALCON-045–051) updates
it, and every new command carries the current value.

`CommandResult` has `command`, `outcome`, `response`, `problems`, and the
shortcuts `reason`, `state` and `error`. `outcome` is one of:

| Outcome | Meaning |
| --- | --- |
| `COMPLY` | The follower complied. |
| `DECLINE` | The follower declined. `reason` is the decline reason. |
| `STATUS_RESPONSE` | Reply to `REPORT_STATUS`. `state` is the follower's status. |
| `ERROR` | The follower rejected the message as a protocol error. `error` is `{code, ...}`. |
| `TIMEOUT` | No response before the deadline. |
| `INVALID_RESPONSE` | A response to this command arrived but failed validation, came from the wrong sender, or was addressed elsewhere. `problems` says why. |

## Rules it follows

- **Nothing invalid is sent.** Each command is validated against the protocol
  schema first. A bad command raises `InvalidCommandError` and nothing goes on
  the wire.
  - Examples: an unknown verb, a missing waypoint for `PROCEED_TO_WAYPOINT`, a
    waypoint on any other command, a bad agent id, a negative epoch.
- **Every command gets a new `message_id`** (a UUID), a UTC ISO 8601
  timestamp, `schema_version: 1`, `sender_role: "commander"` and the current
  `role_epoch`. `hierarchy_level` is never added: it is log-only.
- **Responses are matched by `payload.in_reply_to`,** so replies can arrive in
  any order.
  - A valid reply to another outstanding command is held until that command is
    awaited.
  - Heartbeats are kept for `other_messages`, and so are replies nobody is
    waiting for.
  - Those buffers are bounded (`KEEP_PER_FOLLOWER`).
- **A late reply is not taken for a new one.** Once a command times out, a
  reply that turns up later is unsolicited and goes to `other_messages`.
- **A received message never crashes the Commander.**
  - A malformed reply, or a validator that throws, is recorded in
    `rejected_responses`.
  - If the reply answers the command being awaited, that command ends as
    `INVALID_RESPONSE`.
- **Broadcast:** every message is validated before any is sent. A follower
  whose link fails is listed in `failed`, and the others still get the command.
- **Transport failures** other than a timeout, such as a closed link, are
  raised to the caller as `TransportError` subclasses.

One Commander is meant to be used from one thread, matching the transport's
one-receive-loop-per-link guidance.

## Running the Commander agent

`src/agents/commander_runner.py` runs the Commander as its own process over
TCP. Each follower listens with `TCPServerTransport`, and the Commander
connects to it as a client, one link per follower.

```bash
# One follower: send HOLD and wait up to 5 s for the answer
python -m src.agents.commander_runner --follower follower-1=127.0.0.1:5000

# Broadcast to three followers
python -m src.agents.commander_runner   --follower follower-1=follower-1:5000   --follower follower-2=follower-2:5000   --follower follower-3=follower-3:5000

# The same, from the environment (for Docker Compose)
FALCON_FOLLOWERS="follower-1=follower-1:5000,follower-2=follower-2:5000"   python -m src.agents.commander_runner
```

`make run-commander` (`python -m src.agents.commander`) does the same. It
prints a harmless runpy warning, because the package already imports the
module, so containers should use `src.agents.commander_runner`.

| Option | Default | Meaning |
| --- | --- | --- |
| `--follower ID=HOST:PORT` | `FALCON_FOLLOWERS` | A follower to command. Repeat it for several. A host name, such as a Compose service, is resolved to IPv4 before connecting. |
| `--command` | `HOLD` | `HOLD`, `PROCEED_TO_WAYPOINT`, `RETURN` or `REPORT_STATUS`. |
| `--waypoint` | — | Required for `PROCEED_TO_WAYPOINT` only. |
| `--agent-id` | `FALCON_AGENT_ID`, otherwise `commander` | Must match each follower's configured `parent_id`. |
| `--role-epoch` | `0` | Stamped on every command. |
| `--timeout` | `5` | Seconds to wait for **all** responses, measured from the send. |
| `--connect-timeout` | `5` | Seconds per connection attempt. |
| `--connect-retries` | `5` | Extra attempts, with backoff, while followers start up. |

The command is validated before any connection is opened. With several
followers it is a broadcast, one message per follower.

**Output.** There is one JSON line per follower, in the order given, and then a
summary line:

```json
{"command": "HOLD", "follower": "follower-1", "latency_ms": 2.1, "message_id": "…", "outcome": "COMPLY"}
{"command": "HOLD", "follower": "follower-2", "latency_ms": 2.4, "message_id": "…", "outcome": "DECLINE", "reason": "BATTERY_CRITICAL"}
{"summary": {"answered": 2, "command": "HOLD", "followers": 2}}
```

`outcome` is one of the outcomes above, or one of:
- `UNREACHABLE`: no connection after all the retries;
- `SEND_FAILED`: the link broke while sending;
- `LINK_FAILED`: the link broke while waiting.

**Exit status:**
- `0`: every follower answered with a valid response. A `DECLINE` counts,
  because the round trip worked.
- `1`: at least one follower timed out, answered invalidly, or couldn't be
  reached.
- `2`: bad arguments or an invalid command.

This output is a run report, not the exchange log. JSONL exchange logging
(FALCON-039) sits above the transport and is added separately.

## Not done here (later tickets)

| Not done here | Ticket |
| --- | --- |
| Logging sends and receives | FALCON-039 |
| Sending heartbeats | FALCON-061 |
| Changing roles or epochs | FALCON-045–051 |
| Retries | FALCON-072/076 |
| Hierarchy routing | FALCON-055 |

## Tests

`tests/integration/test_commander_runner.py` (31 tests) runs the runner over
real TCP against the Follower module:
- HOLD → COMPLY within the timeout;
- a broadcast to 3 followers;
- a decline at 5% battery, with the follower's state unchanged;
- a timeout and an unreachable follower, without hiding the other answers;
- a follower that starts late;
- host-name resolution;
- bad arguments;
- a run as a separate process.

`tests/integration/test_decline_round_trip.py` (38 tests) runs the Commander
and the Follower together, with the project validator on both sides:
- every decline reason against every command (8 × 4), checking:
  - the Commander gets `DECLINE` with exactly that reason;
  - the reply is valid and correlated;
  - the follower's state is unchanged;
- a check that the decline reasons in the code and in the schema match;
- broadcasts where each follower declines with its own reason, and a mixed
  comply/decline broadcast;
- a follower that complies normally after declining;
- protocol errors that are not declines: a stale epoch, or a command from a
  commander that isn't the follower's parent. Each returns `ERROR` without
  reaching the decision step.

The decisions in these tests are fixed stand-ins. They test how a decline
travels between the two agents, not the decision policy.

`tests/unit/test_commander.py` (48 tests) runs the Commander over
`InMemoryTransport`. It covers:
- every command;
- invalid commands;
- ids, timestamps and epochs;
- each outcome;
- out-of-order replies, heartbeats and late replies;
- hostile responses;
- broadcast, including a dead link;
- the follower registry.

Messages are checked with `src.schema.validator` when it exists, and
otherwise directly against `src/schema/falcon-v1.json`.

```bash
make test
```


## Persistent registered TCP session

Add `--interactive` to use the actual agent session runtime:

```bash
.venv/bin/python -m src.agents.commander_runner --interactive --follower follower-1=127.0.0.1:5000
```

This mode supports one follower, resolves hostnames to IPv4 at the agent layer, registers via
REPORT_STATUS on every connection, and accepts commands from stdin until QUIT
or EOF. PROCEED_TO_WAYPOINT takes its waypoint after a space; RECONNECT is a
local session control, never a wire command. Broken links trigger bounded
recovery and a DELIVERY_UNKNOWN report without replaying the command. Idle
response timeouts alone do not reconnect. Defaults are 1-second initial backoff,
a 30-second cap, and five extra attempts. The startup backoff in one-shot mode
is also aligned to 1 second/cap 30 seconds. See
[transport.md](transport.md#actual-agent-sessions-falcon-028029) for the Follower
command, registration contract, restart demonstration, and acceptance tests.

Interactive Commander sessions resolve Docker service names (for example
`follower`) at the agent boundary on every connection attempt. DNS failures
use the same bounded retry count; reconnect resolves again in case the service
IP changed. The TCP backend remains IPv4-only. DNS uses the system resolver;
the socket connect timeout starts after name resolution.
