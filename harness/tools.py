from __future__ import annotations

import json

from .contracts import ToolCall, ToolResult


class CoreTools:
    """Real bounded integer operations; no eval, filesystem, network, or shell."""

    def schemas(self) -> list[dict]:
        return [{"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": {
                key: {"type": "integer", "minimum": -1000000000, "maximum": 1000000000}
                for key in ("a", "b")}, "required": ["a", "b"], "additionalProperties": False}}}
            for name, description in (("add_integers", "Add two integers."),
                                      ("multiply_integers", "Multiply two integers."))]

    async def execute(self, call: ToolCall) -> ToolResult:
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
    text = json.dumps(result.wire(), ensure_ascii=False)
    if len(text) <= limit:
        return result
    # Keep the wire result valid JSON and explicitly mark lost data.
    preview_size = max(0, limit // 2 - 100)
    bounded = ToolResult(result.ok, data=text[:preview_size],
                         error="output_truncated", truncated=True)
    while len(json.dumps(bounded.wire(), ensure_ascii=False)) > limit and preview_size:
        preview_size //= 2
        bounded = ToolResult(result.ok, data=text[:preview_size],
                             error="output_truncated", truncated=True)
    return bounded
