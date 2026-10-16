from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Status(str, Enum):
    COMPLETED = "completed"
    VERIFICATION_FAILED = "verification_failed"
    INTERRUPTED = "interrupted"
    UNVERIFIED = "unverified"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str

    def wire(self) -> dict:
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": self.arguments}}


@dataclass(frozen=True)
class ModelReply:
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = "stop"

    def message(self) -> dict:
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [call.wire() for call in self.tool_calls]
        return message


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None
    truncated: bool = False

    def wire(self) -> dict:
        return {"ok": self.ok, "data": self.data,
                "error": self.error, "truncated": self.truncated}


class ModelError(Exception):
    """Safe error code; never carry a raw provider body or headers."""

    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
