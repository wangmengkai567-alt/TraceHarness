import asyncio
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.arithmetic import ArithmeticTools
from harness.config import Config
from harness.contracts import ModelError, ModelReply, ToolCall, ToolResult
from harness.runner import Runner
from harness.tool_results import bound_result
from tests.fakes import TOOL_LOOP_TASK, ToolLoopFake


class ScriptModel:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests = []

    async def complete(self, messages, tools):
        self.requests.append(copy.deepcopy(messages))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class SlowModel:
    def __init__(self):
        self.entered = asyncio.Event()
        self.stopped = asyncio.Event()

    async def complete(self, messages, tools):
        self.entered.set()
        try:
            await asyncio.sleep(10)
        finally:
            self.stopped.set()


class SlowTools(ArithmeticTools):
    async def execute(self, call):
        await asyncio.sleep(10)


class LargeTools(ArithmeticTools):
    async def execute(self, call):
        return ToolResult(True, data="a" * 20000)


def call(arguments='{"a":47,"b":58}', id="lookup", name="add_integers"):
    return ModelReply(tool_calls=(ToolCall(id, name, arguments),), finish_reason="tool_calls")


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        self.config = Config(model="test-model", base_url="https://test.invalid/v1", runs_dir=str(self.workspace / "runs"), retry_base_seconds=0.01)

    async def run_model(self, model, *, config=None, tools=None, cancel=None, secrets=(), task=TOOL_LOOP_TASK):
        return await Runner(config or self.config, model, tools=tools, secrets=secrets).run(
            task, self.workspace, cancel)

    def events(self, result):
        return [json.loads(line) for line in (Path(result["run_directory"]) / "events.jsonl")
                .read_text(encoding="utf-8").splitlines()]

    async def test_dependent_tool_loop_and_persistence(self):
        result = await self.run_model(ToolLoopFake())
        self.assertEqual(result["status"], "unverified")
        self.assertFalse(result["verified"])
        self.assertEqual((result["turns"], result["tool_calls"]), (3, 2))
        self.assertIn("315", result["answer"])
        events = self.events(result)
        requests = [e["data"]["messages"] for e in events if e["type"] == "model_requested"]
        self.assertFalse(any(message["role"] == "tool" for message in requests[0]))
        self.assertEqual([messages[-1]["role"] for messages in requests[1:]], ["tool", "tool"])
        self.assertEqual([json.loads(messages[-1]["content"])["data"]["value"]
                          for messages in requests[1:]], [105, 315])
        multiply = next(e for e in events if e["type"] == "tool_requested"
                        and e["data"]["name"] == "multiply_integers")
        self.assertEqual(json.loads(multiply["data"]["arguments"]), {"a": 105, "b": 3})
        self.assertEqual([e["sequence"] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(events[-1]["data"], result)
        self.assertEqual(json.loads((Path(result["run_directory"]) / "result.json")
                                    .read_text(encoding="utf-8")), result)

    async def test_invalid_arguments_are_returned_to_model(self):
        model = ScriptModel(call('{"a":"bad","b":3}'), ModelReply(content="参数无效，已停止。"))
        result = await self.run_model(model)
        feedback = json.loads(model.requests[1][-1]["content"])
        self.assertFalse(feedback["ok"])
        self.assertEqual(feedback["error"], "invalid_integer_arguments")
        self.assertEqual(result["status"], "unverified")

    async def test_invalid_json_and_unknown_tool(self):
        tools = ArithmeticTools()
        for arguments in ('{', '[]', 'null'):
            self.assertFalse((await tools.execute(ToolCall("a", "add_integers", arguments))).ok)
        self.assertEqual((await tools.execute(ToolCall("b", "run_shell", '{}'))).error, "unknown_tool")
        self.assertFalse((await tools.execute(ToolCall("c", "multiply_integers",
                                                       '{"a":true,"b":3}'))).ok)

    async def test_transient_retry_does_not_reexecute_tools(self):
        model = ScriptModel(call(), ModelError("temporary", retryable=True), ModelReply(content="done"))
        result = await self.run_model(model)
        self.assertEqual((result["model_calls"], result["tool_calls"]), (3, 1))
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(model.requests[1], model.requests[2])
        self.assertEqual(sum(e["type"] == "model_retry_scheduled" for e in self.events(result)), 1)

    async def test_retries_exhausted_and_nonretryable(self):
        model = ScriptModel(*(ModelError("temporary", retryable=True) for _ in range(3)))
        result = await self.run_model(model)
        self.assertEqual((result["status"], result["model_calls"]), ("failed", 3))
        result = await self.run_model(ScriptModel(ModelError("model_http_401")))
        self.assertEqual((result["status"], result["model_calls"]), ("failed", 1))

    async def test_turn_and_model_call_limits(self):
        result = await self.run_model(ToolLoopFake(), config=replace(self.config, max_turns=1))
        self.assertEqual(result["reason"], "turn_limit")
        self.assertEqual(result["status"], "budget_exceeded")
        result = await self.run_model(ScriptModel(ModelError("temporary", retryable=True)),
                                      config=replace(self.config, max_model_calls=1))
        self.assertEqual(result["reason"], "model_call_limit")

    async def test_total_timeout_cancels_model(self):
        model = SlowModel()
        # Exercise cancellation of an active request, independently of disk startup latency.
        clock = [0.0]
        with patch("harness.runner.time", SimpleNamespace(monotonic=lambda: clock[0])):
            running = asyncio.create_task(self.run_model(
                model, config=replace(self.config, total_timeout_seconds=0.1)))
            try:
                await asyncio.wait_for(model.entered.wait(), 2)
                clock[0] = 0.2
                result = await asyncio.wait_for(running, 2)
            finally:
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)
        self.assertEqual(result["reason"], "total_timeout")
        self.assertTrue(model.stopped.is_set())
        self.assertLess(result["elapsed_seconds"], 2)

    async def test_request_timeout_is_bounded(self):
        model = SlowModel()
        result = await self.run_model(model, config=replace(self.config, request_timeout_seconds=0.05, max_retries=0))
        self.assertEqual(result["reason"], "model_request_timeout")
        self.assertTrue(model.stopped.is_set())

    async def test_cancel_pending_model(self):
        cancel, model = asyncio.Event(), SlowModel()
        task = asyncio.create_task(self.run_model(model, cancel=cancel))
        await model.entered.wait()
        cancel.set()
        result = await task
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(model.stopped.is_set())
        self.assertEqual(result["tool_calls"], 0)

    async def test_precancelled_and_external_task_cancel(self):
        cancel = asyncio.Event()
        cancel.set()
        model = ScriptModel()
        result = await self.run_model(model, cancel=cancel)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(model.requests, [])
        slow = SlowModel()
        task = asyncio.create_task(self.run_model(slow))
        await slow.entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(slow.stopped.is_set())
        results = [json.loads(p.read_text(encoding="utf-8")) for p in (self.workspace / "runs").glob("*/result.json")]
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["status"] == "cancelled" for r in results))

    async def test_cancel_during_backoff(self):
        cancel = asyncio.Event()
        model = ScriptModel(ModelError("temporary", retryable=True))
        task = asyncio.create_task(self.run_model(model, cancel=cancel,
                                                  config=replace(self.config, retry_base_seconds=5)))
        while not model.requests:
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.02)
        cancel.set()
        result = await task
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["model_calls"], 1)

    async def test_tool_timeout_is_feedback(self):
        model = ScriptModel(call(), ModelReply(content="tool timed out"))
        result = await self.run_model(model, tools=SlowTools(),
                                      config=replace(self.config, tool_timeout_seconds=0.05))
        self.assertEqual(json.loads(model.requests[1][-1]["content"])["error"], "tool_timeout")
        self.assertEqual(result["status"], "unverified")

    async def test_input_and_output_limits(self):
        model = ScriptModel()
        result = await self.run_model(model, config=replace(self.config, max_input_chars=10))
        self.assertEqual(result["reason"], "input_char_limit")
        self.assertEqual(model.requests, [])
        result = await self.run_model(ScriptModel(ModelReply(content="x" * 1000)),
                                      config=replace(self.config, max_output_chars=128))
        self.assertEqual(result["reason"], "model_output_limit")

    async def test_large_tool_output_bounded(self):
        model = ScriptModel(call(), ModelReply(content="done"))
        result = await self.run_model(model, tools=LargeTools())
        wire = model.requests[1][-1]["content"]
        self.assertLessEqual(len(wire), self.config.max_output_chars)
        self.assertTrue(json.loads(wire)["truncated"])
        self.assertEqual(result["status"], "unverified")
        escaped = bound_result(ToolResult(True, data="\x00" * 1000), 128)
        self.assertLessEqual(len(json.dumps(escaped.wire(), ensure_ascii=False)), 128)

    async def test_duplicate_calls_are_not_executed(self):
        model = ScriptModel(call(), call())
        result = await self.run_model(model, config=replace(self.config, max_model_repairs=0))
        self.assertEqual(result["reason"], "duplicate_tool_call_id")
        self.assertEqual(result["tool_calls"], 1)

    async def test_empty_and_incomplete_answers_fail(self):
        for reply, status, reason in ((ModelReply(), "failed", "model_empty_response"),
                                       (ModelReply(content="partial", finish_reason="length"),
                                        "budget_exceeded", "completion_token_limit")):
            result = await self.run_model(ScriptModel(reply), config=replace(self.config, max_turns=1))
            self.assertEqual((result["status"], result["reason"]), (status, reason))
            self.assertEqual(result["model_calls"], 1)
            self.assertIsNone(result["answer"])

    async def test_key_redacted_everywhere(self):
        secret = "local-test-secret-123"
        result = await self.run_model(ScriptModel(ModelReply(content=f"echo {secret}")),
                                      secrets=(secret,), task=f"task {secret}")
        self.assertNotIn(secret, json.dumps(result))
        for path in Path(result["run_directory"]).iterdir():
            self.assertNotIn(secret, path.read_text(encoding="utf-8"))

    async def test_usage_accumulates(self):
        model = ScriptModel(call(), ModelReply(content="done", usage={"total_tokens": 5}))
        result = await self.run_model(model)
        self.assertEqual(result["usage"], {"total_tokens": 5})


if __name__ == "__main__":
    unittest.main()
