"""Executor-owned recovery guards; never automatically replay a side effect."""
from __future__ import annotations

import json
import os

from .contracts import ToolResult

REPAIRABLE_MODEL_ERRORS = {"model_invalid_json", "model_invalid_response", "model_empty_response",
                           "model_incomplete_response", "model_tool_call_limit", "duplicate_tool_call_id"}
UNCERTAIN_ERRORS = {"file_access_error", "tool_timeout", "tool_execution_error",
                    "shell_start_or_control_error", "shell_timeout", "shell_command_failed"}


class Recovery:
    def __init__(self, files):
        self.files = files
        self.rejected = set()
        self.uncertain_commands = set()
        self.uncertain_paths = set()
        self.unresolved = {}
        self.last_failure = None
        self.repeats = 0

    @staticmethod
    def args(call):
        try:
            args = json.loads(call.arguments)
            return args if isinstance(args, dict) else {}
        except (ValueError, TypeError, RecursionError):
            return {}

    def path(self, value):
        try:
            return os.path.normcase(str(self.files.path(value)))
        except Exception:
            return None

    def blocked(self, call):
        args = self.args(call)
        if call.name == "run_command":
            command = args.get("command")
            if isinstance(command, str) and command in self.rejected:
                return ToolResult(False, error="approval_previously_rejected")
            if isinstance(command, str) and command in self.uncertain_commands:
                return ToolResult(False, error="side_effect_uncertain_no_replay")
        if call.name == "write_file" and self.path(args.get("path")) in self.uncertain_paths:
            return ToolResult(False, error="side_effect_uncertain_read_required")
        return None

    def observe(self, call, result):
        args = self.args(call)
        path = self.path(args.get("path")) if "path" in args else None
        if call.name == "read_file" and result.ok and path:
            self.uncertain_paths.discard(path)
            self.unresolved.pop(path, None)
        if call.name == "run_command" and result.error == "approval_reject":
            self.rejected.add(args.get("command"))
        if call.name in {"write_file", "run_command"} and result.error in UNCERTAIN_ERRORS:
            description = {"call_id": call.id, "tool": call.name, "reason": result.error,
                           "note": "Side effects may have occurred; do not blindly replay."}
            if call.name == "write_file" and path:
                self.uncertain_paths.add(path)
                self.unresolved[path] = description
            elif call.name == "run_command" and isinstance(args.get("command"), str):
                self.uncertain_commands.add(args["command"])
                self.unresolved["command:" + args["command"]] = description
        if result.ok:
            if call.name not in {"update_plan", "update_memory"}:
                self.last_failure, self.repeats = None, 0
        else:
            signature = (call.name, json.dumps(args, sort_keys=True, ensure_ascii=False)
                         if args else call.arguments)
            self.repeats = self.repeats + 1 if signature == self.last_failure else 1
            self.last_failure = signature
