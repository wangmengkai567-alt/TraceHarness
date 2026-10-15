"""Test-only responses for deterministic error and transport checks; not a runtime provider."""
import json

from harness.contracts import ModelError, ModelReply, ToolCall


class ToolLoopFake:
    async def complete(self, messages, tools):
        results = [message for message in messages if message["role"] == "tool"]
        if not results:
            return ModelReply(tool_calls=(ToolCall("test_add", "add_integers", '{"a":47,"b":58}'),),
                              finish_reason="tool_calls")
        result = json.loads(results[-1]["content"])
        if not result["ok"] or result["truncated"]:
            raise ModelError("test_tool_failed")
        if len(results) == 1:
            arguments = json.dumps({"a": result["data"]["value"], "b": 3})
            return ModelReply(tool_calls=(ToolCall("test_multiply", "multiply_integers", arguments),),
                              finish_reason="tool_calls")
        return ModelReply(content=str(result["data"]["value"]))
