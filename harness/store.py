from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    """One writer per run. Events are flushed before the next action."""

    def __init__(self, root: Path, secrets: tuple[str, ...] = ()):
        self.run_id = uuid4().hex
        self.directory = root / self.run_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.sequence = 0
        self.secrets = tuple(value for value in secrets if value)

    def redact(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {self.redact(str(key)): self.redact(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [self.redact(item) for item in value]
        return value

    def write_json(self, name: str, payload: dict) -> None:
        destination = self.directory / name
        temporary = destination.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.redact(payload), stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)

    def emit(self, kind: str, **data) -> None:
        event = {"sequence": self.sequence + 1, "run_id": self.run_id,
                 "timestamp": utc_now(), "type": kind, "data": data}
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(self.redact(event), ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.sequence += 1
