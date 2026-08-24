from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


class AuditLog:
    """Append-only JSONL event store; existing bytes are never rewritten."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.sequence = 0

    def read(self) -> Iterable[dict[str, Any]]:
        if not self.path.exists():
            return []
        events = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict) or "sequence" not in event or "event_type" not in event:
                raise ValueError(f"invalid audit event at line {number}")
            events.append(event)
            self.sequence = max(self.sequence, int(event["sequence"]))
        return events

    def append(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.sequence += 1
        event = {"sequence": self.sequence, "event_type": event_type, "payload": payload}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        return event
