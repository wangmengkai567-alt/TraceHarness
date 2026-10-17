"""执行器的失败恢复保护：记住拒绝和不确定副作用，避免重复执行造成额外影响。

Runner 在工具调用前询问 blocked，在调用后通过 observe 更新保护状态。
这里只判断是否允许继续尝试，实际执行和审批由其他模块完成。
"""
from __future__ import annotations

import json
import os

from .contracts import ToolResult

# 模型格式有误时可请求重新生成；下面的操作错误则意味着副作用可能已经发生。
REPAIRABLE_MODEL_ERRORS = {"model_invalid_json", "model_invalid_response", "model_empty_response",
                           "model_incomplete_response", "model_tool_call_limit", "duplicate_tool_call_id"}
UNCERTAIN_ERRORS = {"file_access_error", "tool_timeout", "tool_execution_error",
                    "shell_start_or_control_error", "shell_timeout", "shell_command_failed"}


class Recovery:
    # 保护记录只在当前运行有效；不通过换工具调用 ID 绕过相同命令的拒绝或不确定状态。
    def __init__(self, files):
        # 分别跟踪拒绝的命令、不确定的命令/文件，以及需要在最终结果里报告的未决事项。
        self.files = files
        self.rejected = set()
        self.uncertain_commands = set()
        self.uncertain_paths = set()
        self.unresolved = {}
        self.last_failure = None
        self.repeats = 0

    @staticmethod
    def args(call):
        # 仅用于识别保护对象；完整参数合法性仍由各工具自身判断。
        try:
            args = json.loads(call.arguments)
            return args if isinstance(args, dict) else {}
        except (ValueError, TypeError, RecursionError):
            return {}

    def path(self, value):
        # 转成规范化的真实路径，防止同一文件以不同路径写法绕过恢复保护。
        try:
            return os.path.normcase(str(self.files.path(value)))
        except Exception:
            return None

    def blocked(self, call):
        # 被拒绝的原命令不再提交审批；结果不确定的命令不盲目重放，文件需先重读。
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
        # 按实际返回结果更新保护状态；调用成功不能反推之前所有操作都已经成功。
        args = self.args(call)
        path = self.path(args.get("path")) if "path" in args else None
        if call.name == "read_file" and result.ok and path:
            # 重新读取后已有最新内容和哈希，允许模型据此重新决定如何写入该文件。
            self.uncertain_paths.discard(path)
            self.unresolved.pop(path, None)
        if call.name == "run_command" and result.error == "approval_reject":
            self.rejected.add(args.get("command"))
        if call.name in {"write_file", "run_command"} and result.error in UNCERTAIN_ERRORS:
            # 超时/执行错误不代表“什么都没发生”，必须保留可能已产生副作用的证据。
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
            # 对同一工具和参数的连续失败计数，Runner 达到阈值后终止，避免死循环。
            signature = (call.name, json.dumps(args, sort_keys=True, ensure_ascii=False)
                         if args else call.arguments)
            self.repeats = self.repeats + 1 if signature == self.last_failure else 1
            self.last_failure = signature
