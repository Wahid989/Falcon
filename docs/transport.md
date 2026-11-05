# Transport interface

FALCON-025 provides a synchronous, message-oriented boundary for Commander and
Follower modules. Import the public API from `src.transport`. No new dependencies
are required. TCP is not implemented in this ticket.

## API

`Message = dict[str, JSONValue]`, where JSONValue recursively includes string,
integer, finite float, boolean, null (`None`), list, and string-keyed dictionary.
`Transport` is an abstract base class. Implementations supply:

| Signature | Behavior |
| --- | --- |
| `connect() -> None` | Open the local endpoint. Idempotent while the link remains open. |
| `send(message: Message) -> None` | Deliver an independent snapshot to the peer. Does not wait for processing or a response. |
| `receive(timeout: float) -> Message` | Remove and return the oldest incoming message, or raise an exception. Timeout is in seconds. |
| `close() -> None` | Permanently close the local endpoint and wake receivers. Idempotent, including before connect. |

This ticket defines these semantics through `InMemoryTransport`; future backends
must document backend-specific setup and connection failures while preserving the
message boundary and timeout/error contract.

## In-memory usage

```python
from src.transport import InMemoryTransport, Transport

commander_link, follower_link = InMemoryTransport.pair()
commander: Transport = commander_link
follower: Transport = follower_link
try:
    commander.connect()
    follower.connect()
    commander.send({
        "message_id": "command-1",
        "sender_id": "agent-01",
        "recipient_id": "agent-02",
        "timestamp": "2026-01-01T00:00:00Z",
        "schema_version": 1,
        "sender_role": "commander",
        "role_epoch": 0,
        "type": "command",
        "payload": {"command": "HOLD"},
    })
    command = follower.receive(timeout=0.5)
    follower.send({
        "message_id": "response-1",
        "sender_id": "agent-02",
        "recipient_id": "agent-01",
        "timestamp": "2026-01-01T00:00:01Z",
        "schema_version": 1,
        "sender_role": "follower",
        "role_epoch": 0,
        "type": "response",
        "payload": {"response": "COMPLY", "in_reply_to": command["message_id"]},
    })
    response = commander.receive(timeout=0.5)
    assert response["payload"]["in_reply_to"] == command["message_id"]
finally:
    commander.close()
    follower.close()
```

## Lifecycle, delivery, and concurrency

- `pair()` returns two initially disconnected endpoints. Connect each explicitly;
  connecting never waits for the peer. Constructing a standalone endpoint is not
  useful: its `connect()` raises `TransportNotConnectedError`.
- Both endpoints must connect before `send()`. A connected endpoint can wait in
  `receive()` before its peer connects; its deadline still applies.
- Each direction has an independent FIFO queue. A successful send means the
  snapshot was enqueued, not that the peer accepted or acted on the message.
- Nested lists/dictionaries are copied on every send, including repeated object
  references. Sender mutations after send and receiver mutations cannot affect
  other deliveries. Do not mutate a message concurrently with its own send.
- A shared condition lock serializes queue and lifecycle operations. Waiting
  receivers wake on send and on either endpoint closing. Concurrent sends are
  ordered by lock acquisition; concurrent receivers compete for each message.
  Use one receive loop per endpoint when agent-level ordering matters.
- Closing locally discards the local inbox and prevents all further operations
  except repeated close. Closing the peer prevents sends, but the still-open
  local endpoint may drain messages already in its inbox before receive reports
  closure. Close wakes all waiters; it does not enqueue a message or sentinel.
- Closure is terminal for the pair. Reconnect requires a new pair. A send racing
  with close either enqueues before close or raises; local close may discard an
  already-enqueued message. There is no delivery acknowledgement or persistence.

## Timeouts and exceptions

`receive()` requires an explicit finite timeout between zero and
`threading.TIMEOUT_MAX`, inclusive. Zero polls without waiting. Negative values,
NaN, infinity, booleans, non-numbers, and values above that platform limit raise
`ValueError`. Deadlines use a monotonic clock and are recalculated after wakeups;
thread scheduling can delay return beyond the nominal deadline. An available
message wins over timeout. Local closure wins over queued messages; peer closure
is reported after draining the inbox. Invalid timeout arguments are checked first.

All operational transport exceptions inherit `TransportError`:

| Exception | Meaning and caller action |
| --- | --- |
| `TransportNotConnectedError` | Local endpoint or send peer not connected; finish setup. |
| `TransportClosedError` | Local endpoint closed, peer closed, or connect attempted on a closed link; stop using the pair. |
| `TransportTimeoutError` | No message before the deadline; caller may receive again. No disconnection is inferred. |
| `TransportMessageError` | Send input is not a JSON object, contains unsupported values/non-string keys/nonfinite floats, cycles, or excessive nesting; fix input. Nothing is queued. |

No transport exception is a wire `ERROR` response. Operational failures are local
Python exceptions; protocol error replies are the agent/schema layer's concern.
JSON compatibility checks prevent Python-only values crossing this boundary;
they do not validate the FALCON schema. Even `{}` is transportable. Strings and
payload fields are not interpreted, routed, modified, or correlated.

## Commander/Follower handoff

Inject a `Transport` into each module rather than constructing a backend inside
agent logic. Runtime/bootstrap code creates links and controls their lifetime;
use `try/finally` to close. Keep one link per peer relationship. Both directions
carry commands, replies, role messages, and heartbeats through the same API.

The Commander constructs and validates envelopes before send. The Follower
validates received envelopes, checks authority, makes decisions, and builds
responses. Both modules own response correlation using `message_id` and
`payload.in_reply_to`; receiving returns the next message, which may be a
heartbeat or unrelated response. Transport does not retry or deduplicate.

The Follower runtime accepts an injected `Transport`, message validator, and
decision callable. It uses `FollowerState.parent_id` to authorize its
commander, passes the current state, command, payload, and message epoch to the
decision callable, then applies the deterministic transition only to compliant
commands. The decision contract is defined in `src/decision/base.py`; it does
not implement command policy. Until FALCON-019 supplies
`src/schema/validator.py`, callers must inject a validator.

The Commander/Follower round-trip tests use paired in-memory transports and
inject a test validator backed by `src/schema/falcon-v1.json` with JSON Schema
format checking. Their fixed comply/decline callbacks exercise message flow
and state transitions only; they do not implement or verify the Phase-4
decision rules.

Logging belongs above the transport boundary: record successful sends/receives
there, exclude heartbeats from exchange logs, and add `hierarchy_level` only to
log records. Transport never adds it to a message. Timeouts are inputs to the
agent's heartbeat/recovery policy, not automatic failover or proof of peer death.

## Limitations and later work

The in-memory backend is process-local, thread-safe, and unbounded; it models
whole JSON messages, not byte streams, capacity limits, backpressure, network
faults, or durability. No sockets, serialization/framing, address configuration,
connection negotiation, or agent runtime wiring are included.

Tickets 026–028 add TCP and byte-stream framing/partial-read behavior behind the
interface. Later transport tickets add their planned runtime integration and
network tests. FALCON-031 proves the single-pair MVP round trip. FALCON-039–041
implement JSONL exchange/event logging and hierarchy enrichment. Duplicate
handling, retries, heartbeat recovery, and broader robustness tests remain with
their later tickets (including FALCON-072–078); they are not hidden in this API.

Run `make test` and `make lint` in the project's Python 3.12 virtual environment.
`tests/unit/test_transport.py` covers round trips, per-direction FIFO, nested
snapshot isolation, timeout recovery, invalid input, lifecycle, and receivers
already blocked when send or close occurs.
