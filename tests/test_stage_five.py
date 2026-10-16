import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from harness.config import Config
from harness.contracts import ModelError, ModelReply, ToolCall, ToolResult
from harness.files import FileTools, digest
from harness.models import OpenAICompatibleModel
from harness.processes import process_alive
from harness.runner import Runner
from harness.web import RunManager, create_app
from tests.test_runner import ScriptModel
from tests.test_stage_two import action, plan
from tests.test_stage_four import command


class StageFiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        self.config = Config(model="test", base_url="https://test.invalid/v1", max_turns=12,
                             runs_dir=str(self.workspace / "runs"), retry_base_seconds=0.001)
        self.file_check = {"id": "content", "type": "file_contains", "path": "result.txt", "text": "correct"}

    async def run_model(self, *steps, config=None, **kwargs):
        model = ScriptModel(*steps)
        result = await Runner(config or self.config, model, **kwargs).run("perform task", self.workspace)
        return result, model

    def events(self, result):
        return [json.loads(line) for line in (Path(result["run_directory"]) / "events.jsonl").read_text(encoding="utf-8").splitlines()]

    def file_config(self, **kwargs):
        return replace(self.config, checks=[self.file_check], **kwargs)

    async def test_format_error_feedback_and_recovery_without_tool_execution(self):
        result, model = await self.run_model(ModelError("model_invalid_json"), ModelReply(content="fixed"))
        self.assertEqual(result["status"], "unverified")
        self.assertEqual((result["turns"], result["model_calls"], result["tool_calls"]), (2, 2, 0))
        self.assertIn("model_invalid_json", json.dumps(model.requests[1]))
        self.assertTrue(any(e["type"] == "model_repair_requested" for e in self.events(result)))

    async def test_malformed_batch_never_executes_valid_prefix(self):
        good = ToolCall("one", "add_integers", '{"a":1,"b":2}')
        malformed = ModelReply(tool_calls=(good, good), finish_reason="tool_calls")
        result, _ = await self.run_model(malformed, ModelReply(content="fixed"))
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["tool_calls"], 0)

    async def test_model_repair_budget_and_final_turn(self):
        result, _ = await self.run_model(*(ModelError("model_invalid_response") for _ in range(3)))
        self.assertEqual(result["reason"], "model_invalid_response")
        self.assertEqual(result["model_calls"], 3)
        result, _ = await self.run_model(ModelError("model_invalid_response"), config=replace(self.config, max_turns=1))
        self.assertEqual(result["model_calls"], 1)

    async def test_auth_failure_does_not_retry_or_repair(self):
        result, _ = await self.run_model(ModelError("model_http_401"))
        self.assertEqual((result["status"], result["model_calls"]), ("failed", 1))

    async def test_transport_configuration_error_is_not_retried(self):
        async def handler(request):
            raise httpx.UnsupportedProtocol("sensitive details")
        model = OpenAICompatibleModel(self.config, "dummy", httpx.MockTransport(handler))
        result = await Runner(self.config, model).run("task", self.workspace)
        self.assertEqual(result["reason"], "model_transport_configuration_error")
        self.assertEqual(result["model_calls"], 1)

    async def test_invalid_arguments_can_be_corrected(self):
        result, model = await self.run_model(action("add_integers", {"a": "bad", "b": 2}, 1),
            action("add_integers", {"a": 1, "b": 2}, 2), ModelReply(content="3"))
        self.assertEqual(result["status"], "unverified")
        self.assertIn("invalid_integer_arguments", json.dumps(model.requests[1]))
        self.assertEqual(result["operations"][1]["ok"], True)

    async def test_repeated_same_failure_stops_even_with_plan_updates(self):
        bad = lambda i: action("add_integers", {"a": "bad", "b": 2}, i)
        result, _ = await self.run_model(bad(1), action("update_plan", plan(), 2), bad(3), bad(4), ModelReply(content="unreachable"))
        self.assertEqual(result["reason"], "repeated_tool_failure")
        self.assertEqual(result["tool_calls"], 4)

    async def test_total_tool_budget_counts_bad_calls(self):
        result, _ = await self.run_model(action("unknown", {}, 1), action("unknown2", {}, 2),
            config=replace(self.config, max_tool_calls=1))
        self.assertEqual((result["status"], result["reason"]), ("budget_exceeded", "tool_call_limit"))
        self.assertEqual(result["tool_calls"], 1)

    async def test_transient_model_retry_after_write_does_not_replay_write(self):
        with patch.object(FileTools, "write", autospec=True, wraps=None) as write:
            write.return_value = {"path": "result.txt", "sha256": "a" * 64, "bytes": 7}
            result, model = await self.run_model(action("update_plan", plan(), 1),
                action("write_file", {"path": "result.txt", "content": "correct", "expected_hash": None}, 2),
                ModelError("model_http_503", retryable=True), ModelReply(content="done"))
        self.assertEqual(write.call_count, 1)
        self.assertEqual(model.requests[-1], model.requests[-2])
        self.assertEqual(result["artifacts"][0]["path"], "result.txt")

    async def test_uncertain_write_requires_actual_read_before_replay(self):
        original = FileTools.write
        count = [0]
        def uncertain(files, path, content, expected_hash):
            count[0] += 1
            observed = original(files, path, content, expected_hash)
            if count[0] == 1:
                raise OSError("lost confirmation after fsync")
            return observed
        write = {"path": "result.txt", "content": "correct", "expected_hash": None}
        with patch.object(FileTools, "write", uncertain):
            result, model = await self.run_model(action("update_plan", plan(), 1),
                action("write_file", write, 2), action("write_file", write, 3),
                action("read_file", {"path": "result.txt"}, 4),
                action("write_file", {**write, "expected_hash": digest(b"correct")}, 5), ModelReply(content="done"))
        self.assertEqual(count[0], 2)
        self.assertIn("side_effect_uncertain_read_required", json.dumps(model.requests[3]))
        self.assertEqual(result["unresolved"], [])

    async def test_uncertain_side_effect_is_reported_without_replay(self):
        def uncertain(files, path, content, expected_hash):
            (files.root / path).write_bytes(content.encode())
            raise OSError("confirmation lost")
        with patch.object(FileTools, "write", uncertain):
            result, _ = await self.run_model(action("update_plan", plan(), 1),
                action("write_file", {"path": "result.txt", "content": "correct", "expected_hash": None}, 2),
                ModelReply(content="write outcome uncertain"))
        self.assertEqual(result["unresolved"][0]["reason"], "file_access_error")
        self.assertEqual((self.workspace / "result.txt").read_bytes(), b"correct")

    async def test_verification_fail_then_repair_plan_and_pass(self):
        result, model = await self.run_model(ModelReply(content="premature done"),
            action("update_plan", plan(), 1),
            action("write_file", {"path": "result.txt", "content": "correct", "expected_hash": None}, 2),
            action("update_plan", plan("completed"), 3), ModelReply(content="repaired"), config=self.file_config())
        self.assertEqual((result["status"], result["verified"]), ("completed", True))
        self.assertEqual(result["verification_attempts"], 2)
        self.assertEqual(result["checks"][0]["sha256"], digest(b"correct"))
        self.assertIn("verification failed", json.dumps(model.requests[1]))
        self.assertEqual(result["unfinished_steps"], [])
        self.assertEqual([e["data"]["passed"] for e in self.events(result) if e["type"] == "verification_finished"], [False, True])

    async def test_check_failure_cannot_be_claimed_as_success(self):
        result, _ = await self.run_model(ModelReply(content="Everything passed!"), ModelReply(content="Everything passed!"), config=self.file_config())
        self.assertEqual((result["status"], result["verified"]), ("verification_failed", False))
        self.assertEqual(result["checks"][0]["error"], "not_a_regular_file")
        self.assertEqual(result["verification_attempts"], 2)

    async def test_no_checks_stays_unverified_even_with_completed_plan(self):
        result, _ = await self.run_model(action("update_plan", plan("completed"), 1), ModelReply(content="all passed"))
        self.assertEqual((result["status"], result["verified"], result["checks"]), ("unverified", False, []))

    async def test_last_turn_failed_check_is_terminal(self):
        result, _ = await self.run_model(ModelReply(content="done"), config=self.file_config(max_turns=1))
        self.assertEqual((result["status"], result["model_calls"]), ("verification_failed", 1))

    async def test_shell_check_without_approval_broker_never_executes(self):
        config = replace(self.config, max_verification_attempts=1,
                         checks=[{"id": "cmd", "type": "shell", "command": "echo harmless", "timeout_seconds": 1}])
        result, _ = await self.run_model(ModelReply(content="done"), config=config)
        self.assertEqual(result["status"], "verification_failed")
        self.assertEqual(result["checks"][0]["result"]["error"], "unknown_tool")

    async def test_rejected_command_is_not_repeated_or_treated_as_temporary(self):
        approvals = []
        async def reject(spec):
            approvals.append(spec)
            return "reject"
        args = {"command": "echo harmless", "timeout_seconds": 1}
        result, _ = await self.run_model(action("run_command", args, 1), action("run_command", args, 2),
            ModelReply(content="rejected"), approve=reject)
        self.assertEqual(len(approvals), 1)
        self.assertEqual(result["operations"][1]["error"], "approval_previously_rejected")

    async def test_uncertain_shell_is_reported_and_not_replayed(self):
        approvals = []
        async def approve(spec):
            approvals.append(spec)
            return "approve"
        with patch("harness.shell.run_process", return_value={"exit_code": None, "output": "", "timed_out": True, "truncated": False, "output_bytes": 0}) as process:
            args = {"command": "echo harmless", "timeout_seconds": 1}
            result, _ = await self.run_model(action("run_command", args, 1), action("run_command", args, 2),
                ModelReply(content="uncertain"), approve=approve)
        self.assertEqual((len(approvals), process.call_count), (1, 1))
        self.assertTrue(result["unresolved"])

    async def test_live_owner_probe_does_not_terminate_process(self):
        self.assertTrue(process_alive(os.getpid()))
        self.assertTrue(process_alive(os.getpid()))

    async def test_check_configuration_validation(self):
        invalid = [[{**self.file_check, "path": "../outside"}], [self.file_check, self.file_check],
                   [{**self.file_check, "text": ""}], [{"id": "x", "type": "shell", "command": "x", "timeout_seconds": True}],
                   [{"id": "x", "type": "shell", "command": "x", "timeout_seconds": 61}]]
        for checks in invalid:
            with self.assertRaises(ValueError):
                replace(self.config, checks=checks).validate()

    async def test_web_check_retry_requires_fresh_approval_and_exposes_report(self):
        self.workspace.joinpath("check.py").write_text(
            "from pathlib import Path\nimport sys\np=Path('attempt'); n=int(p.read_text())+1 if p.exists() else 1\np.write_text(str(n),encoding='utf-8',newline='\\n')\nsys.exit(0 if n==2 else 7)\n", encoding="utf-8", newline="\n")
        cmd = command("check.py") + ("; exit $LASTEXITCODE" if os.name == "nt" else "")
        config = replace(self.config, checks=[{"id": "cmd", "type": "shell", "command": cmd, "timeout_seconds": 10}])
        model = ScriptModel(ModelReply(content="first"), ModelReply(content="second"))
        manager = RunManager(config, lambda: model, approval_timeout=10)
        app = create_app(manager)
        approvals = []
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
                session = (await client.get("/api/session")).json()
                client.headers.update({"X-Harness-Token": session["token"], "Origin": "http://127.0.0.1:8765"})
                run_id = (await client.post("/api/runs", json={"task": "check", "workspace": str(self.workspace)})).json()["run_id"]
                async with asyncio.timeout(20):
                    while not manager.active.job.done():
                        snap = (await client.get(f"/api/runs/{run_id}")).json()
                        approval = snap["approval"]
                        if approval and approval["status"] == "pending":
                            self.assertNotIn(approval["id"], approvals)
                            approvals.append(approval["id"])
                            self.assertEqual(approval["command"], cmd)
                            response = await client.post(f"/api/runs/{run_id}/approvals/{approval['id']}", json={"decision": "approve"})
                            self.assertEqual(response.status_code, 200)
                        await asyncio.sleep(0.01)
                snap = (await client.get(f"/api/runs/{run_id}")).json()
            self.assertEqual(snap["status"], "completed")
            self.assertTrue(snap["checks"]["verified"])
            self.assertEqual(len(approvals), 2)
            self.assertEqual(self.workspace.joinpath("attempt").read_text(), "2")
            self.assertEqual(len(snap["result"]["operations"]), 2)
            events = self.events(snap["result"])
            self.assertEqual([e["data"]["result"]["data"]["exit_code"] for e in events if e["type"] == "tool_responded"], [7, 0])
        finally:
            await manager.shutdown()

    async def test_cancel_pending_verification_retains_partial_evidence(self):
        config = replace(self.config, checks=[{"id": "cmd", "type": "shell", "command": "echo harmless", "timeout_seconds": 1}])
        manager = RunManager(config, lambda: ScriptModel(ModelReply(content="done")))
        try:
            run_id = (await manager.start({"task": "check", "workspace": str(self.workspace)}))["run_id"]
            async with asyncio.timeout(5):
                while not manager.active.approval:
                    await asyncio.sleep(0.01)
            await manager.cancel(run_id)
            await asyncio.wait_for(manager.active.job, 5)
            result = manager.snapshot(run_id)["result"]
            self.assertEqual(result["status"], "cancelled")
            self.assertFalse(result["verified"])
            self.assertEqual(result["checks"][0]["status"], "interrupted")
            self.assertEqual(result["operations"][0]["error"], "operation_interrupted_or_outcome_unknown")
            self.assertFalse(any(e["type"] == "shell_process_started" for e in self.events(result)))
        finally:
            await manager.shutdown()

    async def test_verification_timeout_cannot_pass(self):
        self.workspace.joinpath("slow.py").write_text("import time\ntime.sleep(60)\n", encoding="utf-8", newline="\n")
        config = replace(self.config, max_verification_attempts=1, checks=[{
            "id": "timeout", "type": "shell", "command": command("slow.py"), "timeout_seconds": 1}])
        approvals = []
        async def approve(spec):
            approvals.append(spec)
            return "approve"
        result, _ = await self.run_model(ModelReply(content="done"), config=config, approve=approve)
        self.assertEqual(result["status"], "verification_failed")
        self.assertFalse(result["verified"])
        self.assertEqual(len(approvals), 1)
        self.assertEqual(result["checks"][0]["result"]["error"], "shell_timeout")
        self.assertTrue(any(e["type"] == "shell_process_stopped" for e in self.events(result)))

    async def test_failed_check_repair_does_not_reset_turn_budget(self):
        result, _ = await self.run_model(ModelReply(content="first"), action("update_plan", plan(), 1),
            action("write_file", {"path": "result.txt", "content": "correct", "expected_hash": None}, 2),
            config=self.file_config(max_turns=3))
        self.assertEqual((result["status"], result["reason"]), ("budget_exceeded", "turn_limit"))
        self.assertEqual(result["model_calls"], 3)
        self.assertFalse(self.workspace.joinpath("result.txt").exists())

    async def test_real_http_malformed_response_is_repaired(self):
        count = [0]
        async def handler(request):
            count[0] += 1
            if count[0] == 1:
                return httpx.Response(200, content=b'broken json')
            self.assertIn("model_invalid_json", request.content.decode())
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "fixed"}, "finish_reason": "stop"}]})
        model = OpenAICompatibleModel(self.config, "dummy", httpx.MockTransport(handler))
        result = await Runner(self.config, model).run("task", self.workspace)
        self.assertEqual((result["status"], result["model_calls"]), ("unverified", 2))

    async def test_check_file_mismatch_and_directory_boundary(self):
        self.workspace.joinpath("result.txt").write_bytes(b"wrong")
        result, _ = await self.run_model(ModelReply(content="done"), config=self.file_config(max_verification_attempts=1))
        self.assertEqual(result["checks"][0]["error"], "required_text_missing")
        self.assertEqual(result["status"], "verification_failed")
