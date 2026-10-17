"""逐次审批的 Shell 工具；仅在 Web 提供审批代理时向模型开放。

流程：校验参数 → 固定执行参数 → 等待用户审批 → 执行并清理进程树 → 返回结果。
审批不等于沙箱隔离；批准后的命令可访问工作目录之外的资源。
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

from .contracts import ToolResult
from .files import FileTools
from .processes import run_process
from .tools import CoreTools


class ShellTools(CoreTools):
    # 通过组合 core 保留其他工具，只有 run_command 在这里处理。
    def __init__(self, workspace, config, approve, emit, cancel, core=None):
        # Windows 使用系统 PowerShell，其他平台使用 /bin/sh；工作目录由运行入口指定。
        self.workspace, self.config = str(workspace), config
        self.approve, self.emit, self.cancel = approve, emit, cancel
        self.core = core if core is not None else CoreTools()
        self.shell = (str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
                      if os.name == "nt" else "/bin/sh")

    def schemas(self):
        # 每次命令、重试、验收均需新审批，不根据命令看起来是否只读而跳过。
        return self.core.schemas() + [FileTools.schema(
            "run_command", "Run a shell command in the run workspace. Every call needs explicit user approval; "
            "retries and verification commands also need new approval. This is not a sandbox.",
            {"command": {"type": "string", "minLength": 1, "maxLength": 8000},
             "timeout_seconds": {"type": "number", "exclusiveMinimum": 0,
                                 "maximum": self.config.shell_timeout_seconds}},
            ["command", "timeout_seconds"])]

    async def execute(self, call):
        # 非 Shell 调用继续传给下一层工具；非法命令参数会直接拒绝，不进入审批。
        if call.name != "run_command":
            return await self.core.execute(call)
        try:
            args = json.loads(call.arguments)
            if not isinstance(args, dict) or set(args) != {"command", "timeout_seconds"}:
                raise ValueError
            command, timeout = args["command"], args["timeout_seconds"]
            if (not isinstance(command, str) or not command.strip() or len(command) > 8000 or "\0" in command
                    or type(timeout) not in (int, float) or not math.isfinite(timeout)
                    or not 0 < timeout <= self.config.shell_timeout_seconds):
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            return ToolResult(False, error="invalid_shell_arguments")
        argv = ((self.shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command)
                if os.name == "nt" else (self.shell, "-c", command))
        # 先固定完整执行参数；审批展示的命令、argv、目录和时限与实际执行使用同一份值。
        spec = {"call_id": call.id, "command": command, "shell": self.shell,
                "argv": list(argv), "workspace": self.workspace, "timeout_seconds": timeout}
        decision = await self.approve(spec)
        if decision != "approve":
            return ToolResult(False, error="approval_" + decision)
        if self.cancel.is_set():
            # 防止用户批准和任务取消几乎同时发生时仍启动命令。
            return ToolResult(False, error="approval_cancelled")
        try:
            data = await run_process(argv, self.workspace, timeout,
                                     self.config.max_output_chars // 2,
                                     lambda kind, **data: self.emit(kind, call_id=call.id, **data))
        except OSError:
            return ToolResult(False, error="shell_start_or_control_error")
        result = ToolResult(not data["timed_out"] and data["exit_code"] == 0, data=data,
                          error="shell_timeout" if data["timed_out"] else
                          "shell_command_failed" if data["exit_code"] else None,
                          truncated=data["truncated"])
        # JSON 转义控制字符后可能膨胀到六倍；缩减正文但保留退出码、超时等结构化证据。
        while data["output"] and len(json.dumps(result.wire(), ensure_ascii=False)) > self.config.max_output_chars:
            data["output"] = data["output"][:len(data["output"]) // 2]
            data["truncated"] = True
            result = ToolResult(result.ok, data=data, error=result.error, truncated=True)
        return result
