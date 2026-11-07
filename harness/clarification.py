"""Web-only clarification tool; waiting and answer ownership live in RunManager."""
from __future__ import annotations

import json

from .contracts import ToolResult
from .files import FileTools

MAX_QUESTION_CHARS = 2000
MAX_ANSWER_CHARS = 4000


class ClarificationTools:
    def __init__(self, core, ask):
        self.core, self.ask = core, ask

    def schemas(self):
        return self.core.schemas() + [FileTools.schema(
            "ask_user", "Ask one text question only when essential information is missing. "
            "This must be the ONLY tool call in the response. The answer continues this run; "
            "it does not approve commands or change configured checks. Do not ask when information is sufficient.",
            {"question": {"type": "string", "minLength": 1, "maxLength": MAX_QUESTION_CHARS}},
            ["question"])]

    async def execute(self, call):
        if call.name != "ask_user":
            return await self.core.execute(call)
        try:
            args = json.loads(call.arguments)
            if (not isinstance(args, dict) or set(args) != {"question"}
                    or not isinstance(args["question"], str) or not args["question"].strip()
                    or len(args["question"]) > MAX_QUESTION_CHARS):
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            return ToolResult(False, error="invalid_question_arguments")
        return await self.ask(call.id, args["question"])
