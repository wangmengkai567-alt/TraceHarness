import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from harness.config import Config
from harness.contracts import ModelReply, ToolCall
from harness.store import RunStore
from harness.tools import CoreTools
from harness.web import DETAIL_LIMIT, MAX_BODY, PAGE_SIZE, RunManager, create_app
from tests.test_runner import ScriptModel, SlowModel
from tests.test_stage_two import action, plan


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.config = Config(model="test", base_url="https://test.invalid/v1",
                             runs_dir=str(self.root / "runs"))
        self.model = ScriptModel(ModelReply(content="done"))
        self.manager = RunManager(self.config, lambda: self.model, ("fake-secret",), approval_timeout=10)
        self.app = create_app(self.manager, 8765)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1:8765")
        token = (await self.client.get("/api/session")).json()["token"]
        self.client.headers.update({"X-Harness-Token": token, "Origin": "http://127.0.0.1:8765"})

    async def asyncTearDown(self):
        await self.manager.shutdown()
        await self.client.aclose()
        self.tmp.cleanup()

    async def start(self, mode="model", task="task"):
        response = await self.client.post("/api/runs", json={"task": task,
                                          "workspace": str(self.workspace), "mode": mode})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["run_id"]

    async def snapshot(self, run_id):
        response = await self.client.get(f"/api/runs/{run_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def finish(self):
        await asyncio.wait_for(asyncio.shield(self.manager.active.job), 3)

    def events(self, run_id):
        return [json.loads(line) for line in (self.manager.root / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()]

    async def test_web_file_task_events_and_refresh_are_read_only(self):
        (self.workspace / "AGENTS.md").write_bytes(b"Include source")
        (self.workspace / "input.txt").write_bytes(b"42")
        payload = "<script>window.pwned=true</script>"
        self.model = ScriptModel(action("update_plan", plan(), 1),
                                 action("read_file", {"path": "input.txt"}, 2),
                                 action("write_file", {"path": "summary.txt", "content": payload, "expected_hash": None}, 3),
                                 action("update_plan", plan("completed"), 4), ModelReply(content="done"))
        run_id = await self.start()
        self.assertRegex(run_id, r"^[0-9]{8}001$")
        await self.finish()
        before = len(self.model.requests)
        first = await self.snapshot(run_id)
        self.assertEqual(first["status"], "unverified")
        self.assertEqual(first["rules"]["content"], "Include source")
        self.assertEqual(first["state"]["plan"][0]["status"], "completed")
        self.assertEqual(first["artifacts"][0]["path"], "summary.txt")
        self.assertFalse(first["checks"]["verified"])
        self.assertEqual(first["result"]["answer"], "done")
        for _ in range(2):
            self.assertEqual((await self.client.get("/")).status_code, 200)
            page = (await self.client.get(f"/api/runs/{run_id}/events?after=0")).json()
            self.assertEqual(page["events"], self.events(run_id))
            self.assertIn(payload, json.dumps(page, ensure_ascii=False))
        self.assertEqual(len(self.model.requests), before)
        page = (await self.client.get(f"/api/runs/{run_id}/events?after={page['cursor']}")).json()
        self.assertEqual(page["events"], [])
        self.assertEqual((self.workspace / "summary.txt").read_text(), payload)
        script = (await self.client.get("/app.js")).text
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("eval(", script)
        self.assertIn("seen.has(event.sequence)", script)

    async def test_single_active_run_and_cancel_stops_new_dispatch(self):
        slow = SlowModel()
        self.model = slow
        run_id = await self.start()
        await slow.entered.wait()
        response = await self.client.post("/api/runs", json={"task": "second", "workspace": str(self.workspace)})
        self.assertEqual(response.status_code, 409)
        response = await self.client.post(f"/api/runs/{run_id}/cancel", json={})
        self.assertTrue(response.json()["cancel_requested"])
        await self.finish()
        self.assertEqual((await self.snapshot(run_id))["status"], "cancelled")
        self.assertTrue(slow.stopped.is_set())
        self.assertFalse(any(e["type"] == "tool_requested" for e in self.events(run_id)))

    async def test_cancel_before_second_tool_in_same_model_reply(self):
        manager = self.manager
        dispatched = []

        class CancelAfterFirst(CoreTools):
            async def execute(self, call):
                dispatched.append(call.id)
                result = await super().execute(call)
                manager.active.cancel.set()
                return result

        self.model = ScriptModel(ModelReply(tool_calls=(ToolCall("one", "add_integers", '{"a":1,"b":2}'),
                                                       ToolCall("two", "add_integers", '{"a":3,"b":4}')),
                                            finish_reason="tool_calls"))
        with patch("harness.runner.CoreTools", CancelAfterFirst):
            run_id = await self.start()
            await self.finish()
        self.assertEqual(dispatched, ["one"])
        self.assertEqual((await self.snapshot(run_id))["status"], "cancelled")
        self.assertEqual(sum(e["type"] == "tool_requested" for e in self.events(run_id)), 1)

    async def test_approval_has_one_valid_decision(self):
        run_id = await self.start("approval_demo")
        snapshot = await self.snapshot(run_id)
        self.assertEqual(snapshot["status"], "awaiting_approval")
        approval_id = snapshot["approval"]["id"]
        path = f"/api/runs/{run_id}/approvals/{approval_id}"
        responses = await asyncio.gather(*(self.client.post(path, json={"decision": "approve"}) for _ in range(2)))
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
        await self.finish()
        events = self.events(run_id)
        self.assertEqual(sum(e["type"] == "approval_decided" for e in events), 1)
        self.assertEqual(sum(e["type"] == "simulated_action_completed" for e in events), 1)
        self.assertEqual((await self.snapshot(run_id))["result"]["reason"], "approval_demo_completed")
        self.assertEqual(self.model.requests, [])
        self.assertEqual(list(self.workspace.iterdir()), [])

    async def test_approval_reject_cancel_expire_and_wrong_binding(self):
        run_id = await self.start("approval_demo")
        approval_id = (await self.snapshot(run_id))["approval"]["id"]
        self.assertEqual((await self.client.post(f"/api/runs/{run_id}/approvals/wrong", json={"decision": "approve"})).status_code, 409)
        self.assertEqual((await self.client.post(f"/api/runs/{run_id}/approvals/{approval_id}", json={"decision": "reject"})).status_code, 200)
        await self.finish()
        self.assertEqual((await self.snapshot(run_id))["result"]["reason"], "approval_demo_rejected")
        self.assertFalse(any(e["type"] == "simulated_action_completed" for e in self.events(run_id)))
        run_id = await self.start("approval_demo")
        approval_id = (await self.snapshot(run_id))["approval"]["id"]
        await self.client.post(f"/api/runs/{run_id}/cancel", json={})
        self.assertEqual((await self.client.post(f"/api/runs/{run_id}/approvals/{approval_id}", json={"decision": "approve"})).status_code, 409)
        await self.finish()
        self.assertEqual((await self.snapshot(run_id))["status"], "cancelled")
        self.manager.approval_timeout = 0.02
        run_id = await self.start("approval_demo")
        await self.finish()
        self.assertEqual((await self.snapshot(run_id))["result"]["reason"], "approval_timeout")

    async def test_deadline_checked_at_decision_not_only_waiter(self):
        run_id = await self.start("approval_demo")
        approval_id = (await self.snapshot(run_id))["approval"]["id"]
        self.manager.active.approval_deadline = 0
        response = await self.client.post(f"/api/runs/{run_id}/approvals/{approval_id}", json={"decision": "approve"})
        self.assertEqual(response.status_code, 409)
        await self.client.post(f"/api/runs/{run_id}/cancel", json={})
        await self.finish()

    async def test_origin_host_token_and_request_validation(self):
        payload = {"task": "task", "workspace": str(self.workspace)}
        for headers in ({"Origin": "https://evil.invalid"}, {"Origin": "null"},
                        {"Host": "evil.invalid:8765"}, {"Sec-Fetch-Site": "cross-site"},
                        {"X-Harness-Token": "wrong"}):
            self.assertEqual((await self.client.post("/api/runs", json=payload, headers=headers)).status_code, 403)
        token = self.client.headers["X-Harness-Token"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1:8765") as client:
            self.assertEqual((await client.post("/api/runs", json=payload, headers={"X-Harness-Token": token})).status_code, 403)
            self.assertEqual((await client.get("/api/runs")).status_code, 403)
        self.assertEqual((await self.client.get("/api/session", headers={"Origin": "https://evil.invalid"})).status_code, 403)
        self.assertEqual((await self.client.post("/api/runs", content="{}", headers={"Content-Type": "text/plain"})).status_code, 415)
        self.assertEqual((await self.client.post("/api/runs", content="{" , headers={"Content-Type": "application/json"})).status_code, 400)
        self.assertEqual((await self.client.post("/api/runs", json=[])).status_code, 400)
        self.assertEqual((await self.client.post("/api/runs", content="x" * (MAX_BODY + 1), headers={"Content-Type": "application/json"})).status_code, 413)
        self.assertEqual((await self.client.post("/api/runs", json={**payload, "mode": []})).status_code, 400)
        self.assertFalse(list(self.manager.root.iterdir()))
        response = await self.client.get("/")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertNotIn("access-control-allow-origin", response.headers)

    async def test_saved_trace_interruption_pagination_and_bounded_output(self):
        store = RunStore(self.manager.root)
        store.write_json("metadata.json", {"task": "interrupted", "workspace": str(self.workspace)})
        self.assertEqual((await self.snapshot(store.run_id))["status"], "interrupted")
        store.emit("model_responded", message={"content": "x" * (DETAIL_LIMIT + 1)})
        for i in range(PAGE_SIZE + 2):
            store.emit("test", number=i)
        self.assertEqual((await self.snapshot(store.run_id))["status"], "interrupted")
        page = (await self.client.get(f"/api/runs/{store.run_id}/events?after=0")).json()
        self.assertTrue(page["has_more"])
        self.assertEqual(len(page["events"]), PAGE_SIZE)
        self.assertTrue(page["events"][0]["data"]["display_truncated"])
        next_page = (await self.client.get(f"/api/runs/{store.run_id}/events?after={page['cursor']}")).json()
        self.assertEqual(len(next_page["events"]), 3)
        self.assertFalse(next_page["has_more"])
        self.assertEqual((await self.client.get(f"/api/runs/{store.run_id}/events?after=-1")).status_code, 400)
        self.assertEqual(len(self.model.requests), 0)

    async def test_legacy_uuid_history_and_new_ids_coexist(self):
        legacy = "a" * 32
        folder = self.manager.root / legacy
        folder.mkdir()
        (folder / "metadata.json").write_text(json.dumps({"run_id": legacy, "task": "old task"}),
                                             encoding="utf-8", newline="\n")
        (folder / "events.jsonl").write_text(json.dumps({"sequence": 1, "run_id": legacy,
                                                      "type": "test", "data": {}}) + "\n",
                                             encoding="utf-8", newline="\n")
        new_id = await self.start()
        await self.finish()
        self.assertEqual((await self.snapshot(legacy))["metadata"]["task"], "old task")
        page = (await self.client.get(f"/api/runs/{legacy}/events?after=0")).json()
        self.assertEqual(page["events"][0]["run_id"], legacy)
        history = (await self.client.get("/api/runs")).json()["runs"]
        self.assertEqual({item["run_id"] for item in history}, {legacy, new_id})

    async def test_disconnect_does_not_cancel_and_shutdown_does(self):
        slow = SlowModel()
        self.model = slow
        run_id = await self.start()
        await slow.entered.wait()
        await self.client.aclose()
        self.assertFalse(self.manager.active.cancel.is_set())
        self.assertFalse(self.manager.active.job.done())
        await self.manager.shutdown()
        self.assertTrue(slow.stopped.is_set())
        self.assertEqual(self.manager.snapshot(run_id)["status"], "cancelled")

    async def test_missing_model_config_is_error_not_demo(self):
        self.manager.model_factory = lambda: (_ for _ in ()).throw(ValueError("missing key"))
        response = await self.client.post("/api/runs", json={"task": "task", "workspace": str(self.workspace)})
        self.assertEqual(response.status_code, 503)
        self.assertFalse(list(self.manager.root.iterdir()))
