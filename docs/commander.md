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

## Not done here (later tickets)

| Not done here | Ticket |
| --- | --- |
| Logging sends and receives | FALCON-039 |
| Sending heartbeats | FALCON-061 |
| Changing roles or epochs | FALCON-045–051 |
| Retries | FALCON-072/076 |
| Hierarchy routing | FALCON-055 |

## Tests

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
