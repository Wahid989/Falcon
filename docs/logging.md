# Exchange, role and event logging

FALCON-039/040/041 extend the MVP logger without changing wire messages or the
storage ingestion contract. The Python agents write files only; they never
call SQL Server or the .NET API. Transport still does not perform logging.

## Writer and lifecycle

```python
from src.logging.exchange import ExchangeLogger

with ExchangeLogger(
    "logs", "agent-a", session_id="demo",
    hierarchy=lambda: role_manager.hierarchy,
    max_bytes=100_000_000, backup_count=10,
    flush_every=1,
) as log:
    log.record("sent", message)
    log.event("CONNECTION_LOST", {"peer_id": "ground"}, severity="WARNING")
```

The writer uses a buffered binary append stream and one reentrant lock per
writer. UTF-8 byte counts, not character counts, determine rotation. The default
flush_every=1 keeps completed records visible immediately to the live ingester
and existing MVP gate. Larger positive values batch records; use flush() when
visibility is needed and close()/a context manager at shutdown. CLI-owned writers
are closed in finally blocks; DualRoleAgent.stop flushes its externally owned
writer. An atexit fallback handles ordinary interpreter shutdown.

Close is idempotent and terminal. Serialization, write, flush and rotation
failures are visible; the logger does not silently discard evidence. A failed
send/log is not permission to replay a command whose delivery is uncertain.
One process/writer owns an agent file; the lock supports multiple threads, not
multiple processes rotating the same filename. There is no per-record fsync or
guarantee against SIGKILL, power loss, or a partial OS write.

## Rotation and retention (039)

Before the next whole record would exceed **100,000,000 bytes**, the writer
flushes/closes the stream, shifts the archives, renames the active file, then
opens a new active file. Default retention:

```text
agent-a.jsonl        active
agent-a.jsonl.1      newest archive
...
agent-a.jsonl.10     oldest retained archive
```

After ten archives, the oldest is intentionally removed. No record is split
between files. A single record larger than the threshold occupies its own file;
it is not truncated. Keep ingestion faster than retention turnover if all
historical data must reach SQL. The ingester already recognizes renamed files
and *.jsonl*; no SQL migration or new dependency is needed.

max_bytes/backup_count/flush_every/buffer_bytes accept positive integers. Tests
use small limits to exercise real rotation without allocating 100 MB per test.

## Dynamic hierarchy levels (040)

An optional hierarchy object or callable resolves levels each time a record is
written. Use a callable when immutable hierarchy snapshots can be replaced.
Own level is current distance to top; received messages use the known sender's
current level. Unknown senders have hierarchy_level=null and unknown_sender=true
in their log wrapper. The wire envelope is never modified.

exchange_logger_for configures a hierarchy-backed logger. DualRoleAgent binds
that logger to the RoleManager's latest hierarchy when present, otherwise its
runtime hierarchy. Consequently a skip-level insertion or failover immediately
changes levels used by subsequent log writes (including an old top's 0 -> 1).
RoleManager transition records retain their transition-time level/timestamp.
This fixes log metadata; it does not claim to open new network links or rewire
agent modules after the hierarchy changes.

## Role and error/event records (041)

write_record(record) persists role_change, event, state_snapshot and message
records in the existing storage format. The writer supplies its own logged_by
and session_id, preventing a record from going to a different agent's file.

DualRoleAgent registers ExchangeLogger.write_record through
RoleManager.add_record_sink. Existing on_record observers remain active; a sink
is attached once and detached on runtime stop. This persists grant/deny,
self-promotion, promotion decline, vacancy, step-down and stale-authority events
already emitted by RoleManager. Role wire requests/replies are also recorded.

The actual agent paths now persist events for invalid commands/messages/responses,
protocol ERROR responses, response timeouts, lost/failed sends, transport errors,
and rejected/spoofed peers. Detail includes IDs, peer/correlation information,
validation problems or error code as available. Heartbeats remain excluded from
exchange records. Ordinary state changes are not automatically snapshotted by
this ticket; callers may use write_record for state_snapshot where required.

Unknown-sender is an additional log-wrapper flag. The existing ingester can still
filter unknown levels via NULL; it ignores unrecognized wrapper fields. Event
severity and RoleManager outcomes retain the existing case-insensitive parser
contract. Schema/wire validation remains the protocol layer's responsibility.

## Verification

```bash
.venv/bin/python -m pytest tests/unit/test_exchange_logging.py tests/unit/test_rotating_logging.py tests/integration/test_role_event_logging.py tests/integration/test_exchange_log_content.py -v
make test
make lint
.venv/bin/python -m scripts.run_mvp
```

Coverage includes buffered close, concurrent appends/rotation, UTF-8 sizes,
retention, restart append, metadata following skip-level insertion, unknown
senders, real role grants, preserved observers and protocol-error context.
The existing real-agent/fixture tests pin normal message format compatibility.

For shared ticket 043, storage ingestion and SQL constraints/concurrency/rollback
remain the existing .NET test suite and scripts.verify_storage. Python/logger
verification alone is not a claim that live SQL ingestion passed on this machine.
