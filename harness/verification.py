"""独立验收：只有操作者配置的检查能作为执行器的验证证据。

模型给出最终回答后由 Runner 调用本模块；失败证据会反馈给模型供其修正。
检查全部通过只证明这些检查条件成立，不表示所有需求均已覆盖。
"""
from __future__ import annotations

import math
from pathlib import Path, PureWindowsPath
from uuid import uuid4
import json

from .contracts import ToolCall
from .files import FileError


def validate_checks(checks, shell_timeout):
    # 启动前验证检查结构、唯一 ID、工作目录相对路径，以及命令和时限是否合法。
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
    # 逐项执行并就地填充 evidence；先记 running，便于中断时仍能识别未完成的检查。
    for spec in checks:
        check_budget()
        item = {"id": spec["id"], "type": spec["type"], "passed": False, "status": "running"}
        evidence.append(item)
        store.emit("check_started", check=spec)
        if spec["type"] == "shell":
            # 固定的验收命令也走常规工具分发、预算和用户审批，每次生成新调用 ID。
            call = ToolCall("check-" + uuid4().hex, "run_command", json.dumps({
                "command": spec["command"], "timeout_seconds": spec["timeout_seconds"]}))
            result = await dispatch(call, purpose="verification")
            item.update(call_id=call.id, result=result.wire(), passed=result.ok)
        else:
            try:
                # 文件包含检查直接读取当前文件，并记录哈希和字节数，不依赖模型自述。
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
        # 没有通过的检查也记录 finished；执行结束和检查通过是两个不同字段。
        store.emit("check_finished", **item)
    return evidence
