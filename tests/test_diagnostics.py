import json
import tempfile
import unittest
from pathlib import Path

from harness.config import Config
from harness.contracts import ModelReply, ToolCall
from harness.diagnostics import LIVE_CHECK_TASK, check_live_evidence, run_live_check
from tests.fakes import ToolLoopFake
from tests.test_runner import ScriptModel


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        self.config = Config(model="test-model", base_url="https://test.invalid/v1",
                             runs_dir=str(self.workspace / "runs"))

    async def test_dependent_tool_loop(self):
        result = await run_live_check(self.config, ToolLoopFake(), self.workspace)
        self.assertTrue(check_live_evidence(result))
        self.assertEqual(result["status"], "unverified")
        self.assertFalse(result["verified"])
        metadata = json.loads((Path(result["run_directory"]) / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["task"], LIVE_CHECK_TASK)

    async def test_answer_without_tools_is_rejected(self):
        result = await run_live_check(self.config, ScriptModel(ModelReply(content="315")), self.workspace)
        self.assertFalse(check_live_evidence(result))

    async def test_calls_without_intermediate_feedback_are_rejected(self):
        model = ScriptModel(ModelReply(tool_calls=(
            ToolCall("add", "add_integers", '{"a":47,"b":58}'),
            ToolCall("multiply", "multiply_integers", '{"a":105,"b":3}')),
            finish_reason="tool_calls"), ModelReply(content="315"))
        result = await run_live_check(self.config, model, self.workspace)
        self.assertEqual(result["tool_calls"], 2)
        self.assertFalse(check_live_evidence(result))

    async def test_each_tool_feedback_must_reach_a_model_request(self):
        result = await run_live_check(self.config, ToolLoopFake(), self.workspace)
        path = Path(result["run_directory"]) / "events.jsonl"
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        requests = [event for event in events if event["type"] == "model_requested"]
        for request in requests[1:]:
            with self.subTest(missing_request=request["sequence"]):
                remaining = [event for event in events if event["sequence"] != request["sequence"]]
                path.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in remaining),
                                encoding="utf-8", newline="\n")
                self.assertFalse(check_live_evidence(result))

    async def test_wrong_answer_or_unsuccessful_status_is_rejected(self):
        result = await run_live_check(self.config, ToolLoopFake(), self.workspace)
        for override in ({"answer": "105"}, {"answer": None}, {"status": "failed"}, {"status": "cancelled"}):
            with self.subTest(override=override):
                self.assertFalse(check_live_evidence({**result, **override}))


if __name__ == "__main__":
    unittest.main()
