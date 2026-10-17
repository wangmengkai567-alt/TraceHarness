"""最基础的整数计算工具，以及所有工具通用的输出长度限制。

schemas 用来向模型声明调用格式，execute 负责实际计算并返回 ToolResult。
"""
from __future__ import annotations

import json

from .contracts import ToolCall, ToolResult


class CoreTools:
    """只进行有界整数运算，不使用 eval，也不访问文件、网络或 Shell。"""

    def schemas(self) -> list[dict]:
        # 加法与乘法使用相同的 a/b 参数结构，仅工具名、描述和计算方式不同。
        return [{"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": {
                key: {"type": "integer", "minimum": -1000000000, "maximum": 1000000000}
                for key in ("a", "b")}, "required": ["a", "b"], "additionalProperties": False}}}
            for name, description in (("add_integers", "Add two integers."),
                                      ("multiply_integers", "Multiply two integers."))]

    async def execute(self, call: ToolCall) -> ToolResult:
        # 模型提供的 JSON 不可信：校验工具名、字段集合、整数类型和数值范围后才计算。
        try:
            args = json.loads(call.arguments)
        except (ValueError, TypeError, RecursionError):
            return ToolResult(False, error="invalid_json_arguments")
        if not isinstance(args, dict):
            return ToolResult(False, error="arguments_must_be_object")
        if call.name not in {"add_integers", "multiply_integers"}:
            return ToolResult(False, error="unknown_tool")
        if set(args) != {"a", "b"} or any(type(value) is not int or abs(value) > 1000000000
                                         for value in args.values()):
            return ToolResult(False, error="invalid_integer_arguments")
        value = args["a"] + args["b"] if call.name == "add_integers" else args["a"] * args["b"]
        return ToolResult(True, data={"value": value})


def bound_result(result: ToolResult, limit: int) -> ToolResult:
    # 限制序列化后的字符数，避免超大工具反馈耗尽下一轮输入预算。
    text = json.dumps(result.wire(), ensure_ascii=False)
    if len(text) <= limit:
        return result
    # 截断的是数据预览，再重新序列化；不直接截断 JSON，以免得到无效协议内容。
    preview_size = max(0, limit // 2 - 100)
    bounded = ToolResult(result.ok, data=text[:preview_size],
                         error="output_truncated", truncated=True)
    while len(json.dumps(bounded.wire(), ensure_ascii=False)) > limit and preview_size:
        preview_size //= 2
        bounded = ToolResult(result.ok, data=text[:preview_size],
                             error="output_truncated", truncated=True)
    return bounded
