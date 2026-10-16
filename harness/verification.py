"""Only operator-configured checks can establish verification evidence."""
from __future__ import annotations

import math
from pathlib import Path, PureWindowsPath
from uuid import uuid4
import json

from .contracts import ToolCall
from .files import FileError


def validate_checks(checks, shell_timeout):
    if not isinstance(checks, list) or len(checks) > 20:
        raise ValueError("checks must be a list of at most 20 checks")
    ids = set()
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("id"), str) or not check["id"].strip():
            raise ValueError("each check requires a nonempty id")
        if check["id"] in ids or len(check["id"]) > 100:
            raise ValueError("check ids must be unique and at most 100 characters")
        ids.add(check["id"])
        if check.get("type") == "file_contains":
            if set(check) != {"id", "type", "path", "text"}:
                raise ValueError("file_contains requires id, type, path and text")
            path = check["path"]
            if (not isinstance(path, str) or not path or "\0" in path or ":" in path
                    or Path(path).is_absolute() or PureWindowsPath(path).drive
                    or ".." in path.replace("\\", "/").split("/")):
                raise ValueError("check path must be relative to workspace")
            if not isinstance(check["text"], str) or not check["text"] or len(check["text"]) > 4000:
                raise ValueError("check text must contain 1 to 4000 characters")
        elif check.get("type") == "shell":
            if set(check) != {"id", "type", "command", "timeout_seconds"}:
                raise ValueError("shell check requires id, type, command and timeout_seconds")
            cmd, timeout = check["command"], check["timeout_seconds"]
            if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > 8000 or "\0" in cmd:
                raise ValueError("invalid check command")
            if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= shell_timeout:
                raise ValueError("check timeout must be positive and within shell_timeout_seconds")
        else:
            raise ValueError("check type must be file_contains or shell")


async def verify(checks, files, dispatch, check_budget, store, evidence):
    for spec in checks:
        check_budget()
        item = {"id": spec["id"], "type": spec["type"], "passed": False, "status": "running"}
        evidence.append(item)
        store.emit("check_started", check=spec)
        if spec["type"] == "shell":
            call = ToolCall("check-" + uuid4().hex, "run_command", json.dumps({
                "command": spec["command"], "timeout_seconds": spec["timeout_seconds"]}))
            result = await dispatch(call, purpose="verification")
            item.update(call_id=call.id, result=result.wire(), passed=result.ok)
        else:
            try:
                observed = files.read(spec["path"])
                item.update(path=spec["path"], sha256=observed["sha256"], bytes=observed["bytes"],
                            passed=spec["text"] in observed["content"])
                if not item["passed"]:
                    item["error"] = "required_text_missing"
            except FileError as error:
                item.update(path=spec["path"], error=str(error))
            except (OSError, ValueError, RuntimeError):
                item.update(path=spec["path"], error="check_file_access_error")
        check_budget()
        item["status"] = "finished"
        store.emit("check_finished", **item)
    return evidence
