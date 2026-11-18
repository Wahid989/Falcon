"""Buffered, rotating JSONL records shared by agents and the storage ingester."""

import atexit
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from threading import RLock

from src.transport import Message


class ExchangeLogError(RuntimeError):
    """A record could not be persisted; the runtime must report failure."""


class ExchangeLogger:
    """One writer per agent file; thread-safe, whole-record rotation.

    Defaults: rotate before 100,000,000 bytes, retain 10 archives, flush each
    record for live ingestion. Set flush_every > 1 to batch buffered writes;
    always close (or use a context manager) at shutdown. No fsync/crash guarantee.
    A hierarchy callable resolves current levels on every record.
    """

    def __init__(
        self,
        directory,
        agent_id,
        *,
        session_id="manual",
        level=None,
        peer_levels=None,
        hierarchy=None,
        max_bytes=100_000_000,
        backup_count=10,
        flush_every=1,
        buffer_bytes=65536,
    ):
        if not isinstance(agent_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", agent_id
        ):
            raise ValueError("Log agent ID must be 1–64 letters, digits, '_' or '-'")
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 64:
            raise ValueError("session_id must contain 1–64 characters")
        for name, value in (
            ("max_bytes", max_bytes),
            ("backup_count", backup_count),
            ("flush_every", flush_every),
            ("buffer_bytes", buffer_bytes),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.path = Path(directory) / f"{agent_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.agent_id, self.session_id = agent_id, session_id
        self.level, self.peer_levels = level, dict(peer_levels or {})
        self.max_bytes, self.backup_count = max_bytes, backup_count
        self.flush_every, self.buffer_bytes = flush_every, buffer_bytes
        self._hierarchy = hierarchy
        self._lock = RLock()
        self._stream = None
        self._size = 0
        self._pending = 0
        self._closed = False
        atexit.register(self.close)

    def bind_hierarchy(self, hierarchy):
        """Bind an object, or a callable returning its latest immutable snapshot."""
        with self._lock:
            self._hierarchy = hierarchy

    def _levels(self, sender=None):
        if not isinstance(sender, str):
            sender = None
        hierarchy = self._hierarchy() if callable(self._hierarchy) else self._hierarchy
        if hierarchy is None:
            return self.level, self.peer_levels.get(sender)
        own = hierarchy.level_of(self.agent_id)
        peer = hierarchy.level_of(sender) if sender in hierarchy else None
        return own, peer

    def record(self, direction: str, message: Message) -> None:
        if direction not in {"sent", "received"}:
            raise ValueError("direction must be sent or received")
        if message.get("type") == "heartbeat":
            return
        with self._lock:
            own, peer = self._levels(message.get("sender_id"))
            record = {
                "record_type": "message",
                "direction": direction,
                "hierarchy_level": own if direction == "sent" else peer,
                "message": message,
            }
            if direction == "received" and peer is None:
                record["unknown_sender"] = True
            self.write_record(record)

    def event(self, event_type, detail, *, severity="ERROR"):
        severity = severity.upper()
        if severity not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("Invalid event severity")
        self.write_record(
            {
                "record_type": "event",
                "event_type": event_type,
                "severity": severity,
                "detail": detail,
            }
        )

    def write_record(self, record):
        """Persist a RoleManager record, event, state snapshot, or message.

        The writer owns identity/session metadata. A supplied transition
        timestamp/level is preserved; other records resolve the current own level.
        """
        if record.get("record_type") not in {
            "message",
            "event",
            "state_snapshot",
            "role_change",
        }:
            raise ValueError("Unsupported log record type")
        if record.get("logged_by", self.agent_id) != self.agent_id:
            raise ValueError("Record belongs to a different logging agent")
        with self._lock:
            if self._closed:
                raise ExchangeLogError("Exchange logger is closed")
            own, _ = self._levels()
            enriched = {
                **record,
                "session_id": self.session_id,
                "logged_by": self.agent_id,
                "logged_at": record.get("logged_at")
                or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "hierarchy_level": record.get("hierarchy_level", own),
            }
            try:
                data = (
                    json.dumps(enriched, ensure_ascii=False, allow_nan=False) + "\n"
                ).encode("utf-8")
                if self._stream is None:
                    self._stream = self.path.open("ab", buffering=self.buffer_bytes)
                    self._size = self.path.stat().st_size
                if self._size and self._size + len(data) > self.max_bytes:
                    self._rotate()
                self._stream.write(data)
                self._size += len(data)
                self._pending += 1
                if self._pending >= self.flush_every:
                    self.flush()
            except (OSError, ValueError, UnicodeError) as exc:
                raise ExchangeLogError("Cannot append exchange log") from exc

    def _rotate(self):
        # Flush before renaming so complete records stay together, also on Windows.
        self.flush()
        self._stream.close()
        self._stream = None
        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        oldest.unlink(missing_ok=True)
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                source.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        self._stream = self.path.open("ab", buffering=self.buffer_bytes)
        self._size = 0

    def flush(self):
        with self._lock:
            try:
                if self._stream is not None:
                    self._stream.flush()
                    self._pending = 0
            except OSError as exc:
                raise ExchangeLogError("Cannot flush exchange log") from exc

    def close(self):
        with self._lock:
            if self._closed:
                return
            self.flush()
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            self._closed = True
            atexit.unregister(self.close)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
