from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

RUN_ID_PATTERN = r"(?:[0-9]{8}[0-9]{3,}|[0-9a-f]{32})"
RUN_ID = re.compile(RUN_ID_PATTERN)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    """One writer per run. Events are flushed before the next action."""

    def __init__(self, root: Path, secrets: tuple[str, ...] = ()):
        root.mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y%m%d")
        daily_id = re.compile(day + r"([0-9]{3,})")
        sequence = max((int(match[1]) for entry in root.iterdir()
                        if (match := daily_id.fullmatch(entry.name))), default=0) + 1
        while True:
            self.run_id = f"{day}{sequence:03d}"
            self.directory = root / self.run_id
            try:
                # Atomic reservation also prevents duplicate IDs across CLI/Web processes.
                self.directory.mkdir(exist_ok=False)
                break
            except FileExistsError:
                sequence += 1
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
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(self.redact(payload), stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)

    def emit(self, kind: str, **data) -> None:
        event = {"sequence": self.sequence + 1, "run_id": self.run_id,
                 "timestamp": utc_now(), "type": kind, "data": data}
        with (self.directory / "events.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(self.redact(event), ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.sequence += 1
