# Transport interface

FALCON-025 defines the synchronous, message-oriented boundary for Commander and
Follower modules. FALCON-026–030 add TCP framing, endpoints, recovery support,
and transport tests. Import the public API from `src.transport`. All backends
use the standard library and existing project dependencies.

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
not implement command policy. The shared validator is now implemented in
`src/schema/validator.py` and is used by default by both agent modules.

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

TCP endpoints, framing, recovery support, and network tests are described below.
The actual agent TCP sessions and registration are described below.
FALCON-031 proves the full single-pair MVP round trip. FALCON-039–041
implement JSONL exchange/event logging and hierarchy enrichment. Duplicate
handling, retries, heartbeat recovery, and broader robustness tests remain with
their later tickets (including FALCON-072–078); they are not hidden in this API.

Run `make test` and `make lint` in the project's Python 3.12 virtual environment.
`tests/unit/test_transport.py` covers round trips, per-direction FIFO, nested
snapshot isolation, timeout recovery, invalid input, lifecycle, and receivers
already blocked when send or close occurs.

## TCP server (FALCON-026)

`TCPServerTransport(host="127.0.0.1", port=0, *, connect_timeout=5.0,
io_timeout=5.0, max_frame_bytes=1048576)` implements `Transport` for one client.
`listen()` returns the bound `(host, port)` without waiting; port zero selects
an available port. `connect()` also calls `listen()` if needed, then accepts one
client within `connect_timeout`. A timed-out accept can be called again. After
accepting the peer, the listening socket is closed; additional clients are not
served by this endpoint. Create one server endpoint per accepted link.

Frames use a 4-byte unsigned big-endian body length followed by UTF-8 JSON.
The length counts bytes, excludes the header, and must be between 1 and the
configured limit. Invalid framing/JSON closes the connection and raises
`TransportMessageError`. `TransportConnectionError` reports bind/setup failures.
No schema validation, automatic response, or agent decision is performed.

## TCP client (FALCON-027)

`TCPClientTransport(host, port, *, connect_timeout=5.0, io_timeout=5.0,
max_frame_bytes=1048576)` connects to the server using `connect()`.
A refused connection raises `TransportConnectionError`; a connection deadline
raises `TransportTimeoutError`. Connected calls to `connect()` are idempotent.
Both endpoints use the same `send`/`receive` interface in either direction.

Hosts are IPv4 literals or `localhost` (mapped to `127.0.0.1`), avoiding unbounded
DNS resolution. IPv6 and DNS hostnames are not supported. Client ports are 1–65535;
server ports are 0–65535. Bind `0.0.0.0` only when accepting non-loopback peers is
intended. There is no authentication or encryption; use a trusted offline network.

Receive timeouts preserve incomplete frames for the next call. Send has a finite
`io_timeout`; on timeout the endpoint closes because the peer may have received
an incomplete frame. Never automatically resend that command: processing status
is uncertain and duplicate handling belongs to the agent layer.

## Two-process demo and buffering (FALCON-028)

From the repo root, in terminal 1:

```bash
.venv/bin/python -m scripts.tcp_demo follower --port 5000
```

After it prints its listening address, in terminal 2:

```bash
.venv/bin/python -m scripts.tcp_demo commander --port 5000
```

The follower prints the received HOLD envelope; the commander prints the scripted
COMPLY response with `in_reply_to`. Both processes close and exit. Start the
second terminal within 10 seconds, or increase `--timeout`. This is a transport
smoke test with schema-valid envelopes, not the Commander/Follower runtimes:
it intentionally bypasses the real agent classes. The demo does not validate,
decide, log exchanges, or implement authority. For actual agent validation,
state transitions, and restart recovery, use the persistent agent commands below.

`FrameBuffer` retains partial headers/bodies across calls and extracts one frame
at a time, leaving combined frames for later receives. Tests split a stream at
every byte boundary and send it byte by byte, including multibyte Unicode.
Inbound duplicate JSON keys, nonfinite numbers, invalid UTF-8, nonobject JSON,
and invalid lengths are rejected. EOF during an incomplete frame raises
`TransportMessageError`; EOF at a message boundary raises `TransportClosedError`.
Complete buffered messages can be drained before EOF is observed.

## Connection recovery (FALCON-029)

`ReconnectingTransport(factory, *, max_retries=5, initial_backoff=1.0,
max_backoff=30.0, on_connect=None)` wraps a factory returning a **fresh** Transport.
`connect()` establishes the first link; `reconnect()` closes the previous link
and starts a new bounded connection window. Explicit `close()` is terminal for
the wrapper and interrupts backoff and TCP accept/read/write/connect operations.
The factory and optional registration callback must themselves be bounded.

An initial attempt plus `max_retries` retries use 1, 2, 4, 8, 16, 30, 30… seconds
between failures by default. The default five retries wait 31 seconds total plus
connection/registration attempt durations; increase retries to reach the cap.
Exhaustion sets `unreachable` and raises `TransportUnreachableError` (a subtype of
`TransportConnectionError`). A later explicit `reconnect()` may recover and clear
that flag. Retry waits and exhaustion are observable via standard Python logging
(`src.transport.reconnect`); the application configures handlers. This is not the
JSONL exchange writer planned in FALCON-039–041.

```python
from src.transport import ReconnectingTransport, TCPClientTransport

link = ReconnectingTransport(
    lambda: TCPClientTransport("127.0.0.1", 5000, connect_timeout=2),
    max_retries=6,
    on_connect=None,  # generic transport only; CommanderSession wires registration
)
# link.connect(); use send/receive; call link.reconnect() after connection loss.
# Always link.close() in a finally block.
```

`on_connect(link)` runs on every successful connection, before normal traffic,
so an application can register/re-register through its own agreed handshake.
No registration message type is invented. `CommanderSession` now wires a
validated REPORT_STATUS/STATUS_RESPONSE handshake into this hook, as documented
below. The older transport-only restart test still uses `{"register": true}`
to test the callback in isolation; agent tests use the real schema and modules.

Send/receive never reconnect or replay automatically. On connection loss, the
runtime catches `TransportClosedError`, invokes `reconnect()`, completes its
registration, and decides what to do about any in-flight command. A receive
idle timeout alone is not a reason to reconnect. Failed reconnection is a
secondary availability signal; heartbeat (FALCON-061) remains the primary
disconnect detector. No role/epoch changes occur inside transport.


## TCP test coverage and handoff status (FALCON-030)

Run from the repository root:

```bash
make test
make lint
# TCP socket/process cases only:
.venv/bin/python -m pytest tests/integration/ -v
```

GitHub Actions uses GNU `timeout 30s python -m pytest tests/ -v` on Ubuntu,
so the current full Python suite, including transport tests, must finish within
30 seconds. The CI budget excludes dependency installation and linting.
No additional pytest plugins, network services, fixed test ports, or sleeps
are needed. Socket tests use loopback, ephemeral ports, finite waits, and cleanup.
The CLI demo alone uses the documented fixed default port for manual use.

- `test_framing.py`: every split boundary, single-byte reads, concatenated
  frames, Unicode byte lengths, malformed JSON, duplicate keys and size limits.
- `test_tcp_server.py` / `test_tcp_client.py`: server setup, client exchange,
  idempotent connect, accept timeout and unavailable peer errors.
- `test_tcp_transport.py`: FIFO, combined frames, partial header/body deadlines,
  invalid bytes/lengths, truncated EOF, draining before EOF, send backpressure,
  close waking accept/receive, lifecycle and address conflicts.
- `test_tcp_processes.py`: independent processes, schema-valid HOLD/COMPLY,
  reply correlation checked in the test rather than inside transport.
- `test_reconnect.py` / `test_tcp_reconnect.py`: capped backoff, bounded attempts,
  observable logs, interruption, no command replay, registration hooks, and real
  server restart recovery. Short test backoffs exercise the same schedule;
  default 1/2/4/8/16/30-second values are verified without waiting minutes.

TCP supports one sending operation and one receiving operation concurrently.
Each direction serializes its callers; a receive's lock wait counts toward its
receive deadline. Prefer one receive loop per endpoint. Connect calls serialize;
its configured deadline starts once that call owns connection setup. Send's
`io_timeout` starts after serialization and lock acquisition; it bounds socket
I/O, not JSON serialization or time spent behind another sender. Close interrupts
socket waits via shutdown and polling at intervals of at most 50 milliseconds,
subject to OS scheduling. Connections are terminal after local close, detected
EOF, malformed input, or a failed send; use a fresh instance to reconnect.

| Ticket | Verified here | Remaining integration |
| --- | --- | --- |
| 026 | Single-client TCP server, framing and intact exchange | Wired into FollowerTCPServer. |
| 027 | TCP client, bounded setup and response exchange | Wired into CommanderSession. |
| 028 | Actual Commander/Follower processes exchange all four commands and apply state transitions; stream split tests pass. | Complete locally; Phase-4 policy remains separately injected. |
| 029 | Actual follower process crash/restart, validated re-registration, subsequent commands, retry exhaustion, and shutdown. | Complete locally; heartbeat/role recovery remain later tickets. |
| 030 | Local socket/process/error coverage and CI time-budget configuration | Confirm the new run on GitHub after review and push. |

No schema types, role/epoch changes, command replay, JSONL exchange writer, or
Docker agent entry points are added here. The actual Follower owns state and
authorization. The Docker/logging MVP is documented in [mvp-running.md](mvp-running.md).


## Actual agent sessions (FALCON-028/029)

`src/agents/tcp_runtime.py` wires the real `Commander` and `Follower` classes to
TCP, using the shared schema validator and the existing state transitions.
`CommanderSession` owns a `ReconnectingTransport`; `FollowerTCPServer` accepts
successive connections and keeps its Follower state for the lifetime of that
process. The existing one-shot/broadcast Commander CLI remains available.
Persistent registration and recovery are enabled explicitly with `--interactive`.

### Registration without changing the v1 schema

On every new connection, including reconnect, CommanderSession performs:

1. Build and validate a fresh REPORT_STATUS command to the configured follower.
2. The real Follower checks schema, recipient, configured parent and epoch,
   invokes the injected policy, and returns its validated STATUS_RESPONSE.
3. Commander checks schema and correlation through its normal response handler,
   then requires the expected follower ID, `sender_role=follower`, state
   `current_role=follower`, and matching envelope/state `role_epoch`.
4. Only after success does the session report `registered` and allow user commands.
   Failure participates in the bounded connection retry window. The state probe
   does not move the follower or change role/epoch. It is a registration exchange
   for this runtime, not a new wire type or cryptographic identity authentication.

The Follower authorizes the configured parent rather than accepting a new parent
from the connection. Its first successful REPORT_STATUS on a connection emits a
registration event. It also remains compatible with legacy one-shot clients
which do not perform a registration probe. No transport backend interprets these
messages. Session identity and schema handling belong exclusively to agents.

### Terminal demonstration with actual agents

From the repo root, start terminal 1:

```bash
.venv/bin/python -m src.agents.follower_runner --agent-id follower-1 --commander-id commander --port 5000 --decision-engine scripts.tcp_agent_policy:decide
```

Then terminal 2:

```bash
.venv/bin/python -m src.agents.commander_runner --interactive --follower follower-1=127.0.0.1:5000 --timeout 2 --connect-timeout 1 --connect-retries 6
```

Both terminals print registration records. In terminal 2 enter one line at a time:

```text
HOLD
PROCEED_TO_WAYPOINT north-fence
REPORT_STATUS
RETURN
REPORT_STATUS
QUIT
```

The Commander prints correlated results. The Follower prints complete requests,
responses, and post-command state. HOLD clears movement/current command;
PROCEED changes position to north-fence; RETURN sets it to base; REPORT_STATUS
returns the real state without mutation. QUIT closes the Commander; the Follower
continues listening for another session until Ctrl+C. EOF on Commander stdin
also closes its session. RECONNECT explicitly opens and registers a new session.

The policy argument is required because the Phase-4 decision engine is not yet
implemented. `scripts.tcp_agent_policy:decide` is a small demo-only injected
policy: it declines movement below 20% battery or with a mechanical fault,
and declines PROCEED when an obstacle is present; otherwise it complies.
It does not claim terrain/capability/busy/role policy. Swap in the eventual real
`MODULE:CALLABLE` without editing transport or the Follower. For a low-battery
demo, add `--state tests/scenarios/follower_low_battery.yml` to terminal 1, then
try RETURN (DECLINE BATTERY_CRITICAL) and HOLD (COMPLY) from terminal 2.

### Demonstrate restart recovery

Keep the interactive Commander running. Stop the Follower in terminal 1 using
Ctrl+C. Enter HOLD in terminal 2: it detects link loss and starts retries. Restart
the identical Follower command in terminal 1 while retries are running.

The Commander re-registers the restarted Follower and reports DELIVERY_UNKNOWN
for the interrupted command with `recovered=true`. Enter a **new** HOLD or
REPORT_STATUS to verify communication has resumed. Never interpret successful
recovery as evidence that the interrupted command was or was not executed.
It is not replayed. The restarted Follower loads fresh initial/YAML state;
in-process reconnects preserve live state, but process persistence is not added.

Retries default to 1, 2, 4, 8, 16, 30… seconds, capped at 30, with one initial
attempt plus `--connect-retries`. The example uses six retries to include the cap.
INFO/WARNING diagnostics go to stderr; JSON result/event records go to stdout.
`--initial-backoff` / `--max-backoff` configure interactive mode for demonstrations
or tests. Exhaustion reports `recovery_failed` with `unreachable=true`; the CLI
stays available so RECONNECT can start a fresh bounded window later.
Ordinary response timeouts return TIMEOUT without triggering recovery. Automatic
loss detection during an otherwise idle connection awaits the heartbeat ticket;
no new election, role-switching, or failover behavior is added here.

`CommandInterruptedError` from the library carries `message_id` when available,
`recovered`, and an optional `recovery_error`. If send failed before returning
its envelope, message_id may be null. A successful reconnect resets old
Commander correlation state; no old pending command crosses a session boundary.
`registered_state` is a copy of the registration snapshot, not a continuously
updated or persistent state cache. Use REPORT_STATUS for current state.

### Verification and file handoff

```bash
.venv/bin/python -m pytest tests/integration/test_agent_tcp_sessions.py tests/integration/test_agent_tcp_registration.py tests/unit/test_tcp_runtime.py -v
make test
make lint
```

The subprocess tests run both actual entry points, all four commands, state
transitions, a battery decline, Follower crash/restart and re-registration,
no command replay, exhaustion followed by explicit recovery, preservation of
live state across reconnect, and clean idle shutdown. Registration tests reject
wrong follower/parent/epoch/role metadata. Unit tests confirm that response
idleness does not reconnect and invalid commands are not sent. Existing frame
split/hostile byte/backpressure tests continue to exercise the TCP backend.

- `src/agents/tcp_runtime.py`: session registration, agent recovery and serving.
- `src/agents/follower_runner.py`: persistent Follower CLI, injected policy/YAML,
  finite polling, and SIGINT/SIGTERM cleanup.
- `src/agents/commander_runner.py`: optional interactive session; existing one-shot
  mode stays intact, with its startup backoff aligned to 1 second/cap 30 seconds.
- `scripts/tcp_agent_policy.py`: explicitly limited demo policy, outside transport.
- `tests/integration/test_agent_tcp_sessions.py`: actual process-level acceptance.
- `tests/integration/test_agent_tcp_registration.py`: rejection and cancellation.
- `tests/unit/test_tcp_runtime.py`: timeout and invalid-command boundaries.


## MVP exchange logging and Compose

The actual Commander/Follower modules now accept an optional `exchange_log`.
The runners enable it with --log-dir/FALCON_LOG_DIR and --session-id/FALCON_SESSION_ID.
The TCP backend still performs no logging. See [mvp-running.md](mvp-running.md)
for the record format, heartbeat exclusion, flat log-only hierarchy levels,
Docker run commands, evidence validation, and limits of this milestone logger.

Interactive Commander sessions resolve Docker service names (for example
`follower`) at the agent boundary on every connection attempt. DNS failures
use the same bounded retry count; reconnect resolves again in case the service
IP changed. The TCP backend remains IPv4-only. DNS uses the system resolver;
the socket connect timeout starts after name resolution.
