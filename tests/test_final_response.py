import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from harness.config import Config
from harness.contracts import ModelError, ModelReply
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
