"""Append-only JSONL exchanges in the storage ingester's record format."""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from threading import Lock

from src.transport import Message


class ExchangeLogError(RuntimeError):
    """An exchange could not be recorded; the runtime must report failure."""


class ExchangeLogger:
    """One writer per agent process/file. Each call appends a complete JSON line."""

    def __init__(
        self, directory, agent_id, *, session_id="manual", level=None, peer_levels=None
    ):
        if not isinstance(agent_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", agent_id
        ):
            raise ValueError("Log agent ID must be 1–64 letters, digits, '_' or '-'")
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 64:
            raise ValueError("session_id must contain 1–64 characters")
        self.path = Path(directory) / f"{agent_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.agent_id = agent_id
        self.session_id = session_id
        self.level = level
        self.peer_levels = dict(peer_levels or {})
        self._lock = Lock()

    def record(self, direction: str, message: Message) -> None:
        if direction not in {"sent", "received"}:
            raise ValueError("direction must be sent or received")
        if message.get("type") == "heartbeat":
            return
        level = (
            self.level
            if direction == "sent"
            else self.peer_levels.get(message.get("sender_id"))
        )
        record = {
            "record_type": "message",
            "session_id": self.session_id,
            "logged_by": self.agent_id,
            "logged_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "direction": direction,
            "hierarchy_level": level,
            "message": message,
        }
        try:
            line = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
            with self._lock, self.path.open("a", encoding="utf-8") as stream:
                stream.write(line)
        except (OSError, ValueError, UnicodeError) as exc:
            raise ExchangeLogError("Cannot append exchange log") from exc
