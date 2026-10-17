import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from harness.config import Config
from harness.contracts import ModelError, ModelReply, ToolCall
from harness.runner import Runner
from tests.test_runner import ScriptModel, SlowModel, call
from tests.test_stage_two import action, plan


class FinalResponseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        self.config = Config(model="test", base_url="https://test.invalid/v1", max_turns=2,
                             runs_dir=str(self.workspace / "runs"), retry_base_seconds=0.01)

    async def run_model(self, model, config=None):
        return await Runner(config or self.config, model).run("answer using the observations", self.workspace)

    async def test_last_response_uses_tool_feedback_and_stays_inside_budget(self):
        outer = self

        class Model(ScriptModel):
            async def complete(self, messages, tools):
                if self.requests:
                    outer.assertEqual(tools, [])
                    outer.assertIn("105", messages[-1]["content"])
                    outer.assertTrue(any("reserved for your final answer" in (m.get("content") or "")
                                         for m in messages))
                else:
                    outer.assertTrue(tools)
                return await super().complete(messages, tools)

        result = await self.run_model(Model(call(), ModelReply(content="The observed result is 105.")))
        self.assertEqual(result["status"], "unverified")
        self.assertIn("105", result["answer"])
        self.assertEqual((result["turns"], result["model_calls"], result["tool_calls"]), (2, 2, 1))
        events = [json.loads(line) for line in (Path(result["run_directory"]) / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        requests = [e["data"] for e in events if e["type"] == "model_requested"]
        self.assertEqual([r["final_response_only"] for r in requests], [False, True])

    async def test_final_response_cannot_dispatch_file_write_even_if_adapter_ignores_tools(self):
        model = ScriptModel(action("update_plan", plan(), 1),
                            action("write_file", {"path": "blocked.txt", "content": "unsafe", "expected_hash": None}, 2))
        result = await self.run_model(model)
        self.assertEqual((result["status"], result["reason"]), ("budget_exceeded", "turn_limit"))
        self.assertFalse((self.workspace / "blocked.txt").exists())
        self.assertEqual(result["tool_calls"], 1)
        self.assertIsNone(result["answer"])

    async def test_completed_plan_and_memory_claim_do_not_replace_answer(self):
        memory = {"goal": "answer", "constraints": [], "progress": ["already answered"],
                  "findings": [], "artifacts": [], "unresolved": []}
        model = ScriptModel(action("update_plan", plan("completed"), 1),
                            action("update_memory", memory, 2), ModelReply(content="Actual answer to user."))
        result = await self.run_model(model, replace(self.config, max_turns=3))
        self.assertEqual(result["answer"], "Actual answer to user.")
        self.assertEqual(len(model.requests), 3)

    async def test_final_request_retries_without_replaying_write(self):
        model = ScriptModel(action("update_plan", plan(), 1),
                            action("write_file", {"path": "once.txt", "content": "once", "expected_hash": None}, 2),
                            ModelError("temporary", retryable=True), ModelReply(content="saved"))
        result = await self.run_model(model, replace(self.config, max_turns=3))
        self.assertEqual(result["status"], "unverified")
        self.assertEqual((result["turns"], result["model_calls"], result["tool_calls"]), (3, 4, 2))
        self.assertEqual((self.workspace / "once.txt").read_bytes(), b"once")
        self.assertEqual(model.requests[-1], model.requests[-2])

    async def test_reserved_response_does_not_extend_model_call_budget(self):
        model = ScriptModel(call(), ModelReply(content="must not be requested"))
        result = await self.run_model(model, replace(self.config, max_model_calls=1))
        self.assertEqual(result["reason"], "model_call_limit")
        self.assertEqual(len(model.requests), 1)

    async def test_final_request_remains_cancellable(self):
        model, cancel = SlowModel(), asyncio.Event()
        task = asyncio.create_task(Runner(replace(self.config, max_turns=1), model).run(
            "answer", self.workspace, cancel))
        await asyncio.wait_for(model.entered.wait(), 3)
        cancel.set()
        result = await asyncio.wait_for(task, 3)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(model.stopped.is_set())
        self.assertEqual(result["tool_calls"], 0)

    async def test_truncation_requests_one_concise_answer_and_counts_rejected_usage(self):
        class Model(ScriptModel):
            async def complete(self, messages, tools):
                if self.requests:
                    self_test.assertEqual(tools, [])
                    self_test.assertTrue(any("concise final answer" in (m.get("content") or "") for m in messages))
                return await super().complete(messages, tools)

        self_test = self
        model = Model(ModelReply(content="discarded partial answer", finish_reason="length",
                                 usage={"prompt_tokens": 7, "completion_tokens": 1024, "total_tokens": 1031}),
                      ModelReply(content="concise answer", usage={"prompt_tokens": 8, "completion_tokens": 10, "total_tokens": 18}))
        result = await self.run_model(model, replace(self.config, max_turns=4, max_completion_tokens=1024))
        self.assertEqual((result["status"], result["answer"]), ("unverified", "concise answer"))
        self.assertEqual((result["turns"], result["model_calls"], result["tool_calls"]), (2, 2, 0))
        self.assertEqual(result["usage"], {"prompt_tokens": 15, "completion_tokens": 1034, "total_tokens": 1049})
        events = [json.loads(line) for line in (Path(result["run_directory"]) / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        error = next(e["data"] for e in events if e["type"] == "model_error")
        self.assertEqual((error["finish_reason"], error["max_completion_tokens"]), ("length", 1024))
        self.assertFalse(any(e["type"] == "model_repair_requested" for e in events))

    async def test_repeated_truncation_stops_without_format_repair_loop(self):
        model = ScriptModel(ModelReply(content="partial", finish_reason="length"),
                            ModelReply(content="partial again", finish_reason="length"),
                            ModelReply(content="must not request"))
        result = await self.run_model(model, replace(self.config, max_turns=5))
        self.assertEqual((result["status"], result["reason"]), ("budget_exceeded", "completion_token_limit"))
        self.assertEqual(len(model.requests), 2)
        self.assertIsNone(result["answer"])

    async def test_truncation_respects_final_turn_and_model_call_budgets(self):
        for limits, reason in (({"max_turns": 1}, "completion_token_limit"),
                               ({"max_turns": 4, "max_model_calls": 1}, "model_call_limit")):
            with self.subTest(limits=limits):
                model = ScriptModel(ModelReply(content="partial", finish_reason="length"), ModelReply(content="not requested"))
                result = await self.run_model(model, replace(self.config, **limits))
                self.assertEqual((result["status"], result["reason"]), ("budget_exceeded", reason))
                self.assertEqual(len(model.requests), 1)

    async def test_truncated_tool_batch_is_not_dispatched_or_replayed(self):
        partial = ToolCall("partial", "write_file", '{"path":"unexpected.txt"')
        model = ScriptModel(action("update_plan", plan(), 1),
                            action("write_file", {"path": "once.txt", "content": "once", "expected_hash": None}, 2),
                            ModelReply(tool_calls=(partial,), finish_reason="length"), ModelReply(content="saved once"))
        result = await self.run_model(model, replace(self.config, max_turns=6))
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["tool_calls"], 2)
        self.assertEqual((self.workspace / "once.txt").read_bytes(), b"once")
        self.assertFalse((self.workspace / "unexpected.txt").exists())
