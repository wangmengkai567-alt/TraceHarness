import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest

from harness.processes import process_alive
from tests.stage_six_support import AcceptanceServer, disk_events, remote_events
from tests.live_stage_six import memory_decision_evidence


class StageSixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.config = {"model": "test", "base_url": "https://test.invalid/v1",
                       "runs_dir": str(self.root / "records"), "approval_timeout_seconds": 5}
        self.workspace.joinpath("marker.py").write_text(
            "from pathlib import Path\np=Path('marker');p.write_bytes(p.read_bytes()+b'x' if p.exists() else b'x')\n",
            encoding="utf-8", newline="\n")

    def pending(self, server, run_id):
        return server.wait(run_id, lambda snap: bool(snap["approval"] and snap["approval"]["status"] == "pending"))

    def approve(self, server, run_id, approval):
        return server.client.post(f"/api/runs/{run_id}/approvals/{approval['id']}", json={"decision": "approve"})

    def assert_cleanup(self, server):
        self.assertTrue(server.cleanup["process_exited"])
        self.assertTrue(server.cleanup["port_released"])
        self.assertFalse(process_alive(server.process.pid))

    def test_real_http_status_matrix_and_saved_traces(self):
        cases = [("request_failure", {}, "failed", "model_http_401"),
                 ("tool_error", {}, "unverified", "invalid_integer_arguments"),
                 ("empty_result", {}, "unverified", None),
                 ("empty_result", {"max_verification_attempts": 1, "checks": [
                     {"id": "missing", "type": "file_contains", "path": "missing.txt", "text": "required"}]}, "verification_failed", "not_a_regular_file"),
                 ("pending", {"approval_timeout_seconds": 0.1}, "unverified", "approval_expired")]
        for scenario, overrides, expected, error in cases:
            with self.subTest(scenario=scenario, expected=expected):
                server = AcceptanceServer({**self.config, **overrides}, self.root, scenario)
                with server:
                    run_id = server.start_run(self.workspace)
                    snapshot = server.wait(run_id)
                    self.assertEqual(snapshot["status"], expected)
                    folder = Path(self.config["runs_dir"]) / run_id
                    events = remote_events(server, run_id)
                    self.assertEqual(events, disk_events(folder))
                    self.assertEqual([e["sequence"] for e in events], list(range(1, len(events) + 1)))
                    if error:
                        self.assertIn(error, json.dumps(events))
                    before = folder.joinpath("events.jsonl").read_bytes()
                    server.client.get("/").raise_for_status()
                    self.assertEqual(server.snapshot(run_id), snapshot)
                    self.assertEqual(before, folder.joinpath("events.jsonl").read_bytes())
                self.assert_cleanup(server)

    def test_refresh_and_duplicate_approval_do_not_repeat_command(self):
        server = AcceptanceServer(self.config, self.root, "pending")
        with server:
            run_id = server.start_run(self.workspace)
            approval = self.pending(server, run_id)["approval"]
            before = remote_events(server, run_id)
            for _ in range(2):
                server.client.get("/").raise_for_status()
                self.assertEqual(server.snapshot(run_id)["approval"], approval)
            self.assertEqual(remote_events(server, run_id), before)
            self.assertEqual(self.approve(server, run_id, approval).status_code, 200)
            self.assertEqual(self.approve(server, run_id, approval).status_code, 409)
            self.assertEqual(server.wait(run_id)["status"], "unverified")
            self.assertEqual(self.workspace.joinpath("marker").read_bytes(), b"x")
        self.assert_cleanup(server)

    def test_reject_and_pending_cancel_never_launch(self):
        for decision in ("reject", "cancel"):
            server = AcceptanceServer(self.config, self.root, "pending")
            with server:
                run_id = server.start_run(self.workspace)
                approval = self.pending(server, run_id)["approval"]
                if decision == "reject":
                    response = server.client.post(f"/api/runs/{run_id}/approvals/{approval['id']}", json={"decision": "reject"})
                else:
                    response = server.client.post(f"/api/runs/{run_id}/cancel", json={})
                response.raise_for_status()
                self.assertEqual(self.approve(server, run_id, approval).status_code, 409)
                snapshot = server.wait(run_id)
                self.assertEqual(snapshot["status"], "cancelled" if decision == "cancel" else "unverified")
                self.assertFalse(self.workspace.joinpath("marker").exists())
                self.assertFalse(any(e["type"] == "shell_process_started" for e in remote_events(server, run_id)))
            self.assert_cleanup(server)

    def test_hard_exit_pending_restart_preserves_trace_and_invalidates_approval(self):
        server = AcceptanceServer(self.config, self.root, "pending")
        with server:
            run_id = server.start_run(self.workspace)
            approval = self.pending(server, run_id)["approval"]
            folder = Path(self.config["runs_dir"]) / run_id
            before = folder.joinpath("events.jsonl").read_bytes()
            server.stop(hard=True)
        self.assert_cleanup(server)
        self.assertFalse(folder.joinpath("result.json").exists())
        # A real crash may leave a partial last event; viewing must ignore it safely.
        with folder.joinpath("events.jsonl").open("ab") as stream:
            stream.write(b'{"sequence":')
        with AcceptanceServer(self.config, self.root, "pending", port=server.port) as restarted:
            snapshot = restarted.snapshot(run_id)
            self.assertEqual(snapshot["status"], "interrupted")
            self.assertIsNone(snapshot["approval"])
            self.assertEqual(self.approve(restarted, run_id, approval).status_code, 409)
            self.assertEqual(remote_events(restarted, run_id), disk_events(folder))
            self.assertEqual(folder.joinpath("events.jsonl").read_bytes(), before + b'{"sequence":')
            self.assertFalse(self.workspace.joinpath("marker").exists())
            next_id = restarted.start_run(self.workspace)
            next_approval = self.pending(restarted, next_id)["approval"]
            self.assertNotEqual(next_approval["id"], approval["id"])
            self.assertEqual(self.approve(restarted, next_id, approval).status_code, 409)
            self.assertEqual(self.approve(restarted, next_id, next_approval).status_code, 200)
            restarted.wait(next_id)
            self.assertEqual(self.workspace.joinpath("marker").read_bytes(), b"x")
        self.assert_cleanup(restarted)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object hard-exit acceptance")
    def test_hard_exit_running_kills_job_tree_and_releases_child_listener(self):
        self.workspace.joinpath("child.py").write_text(
            "import os,time,socket,json\nfrom pathlib import Path\ns=socket.socket();s.bind(('127.0.0.1',0));s.listen()\nPath('child.json').write_text(json.dumps([os.getpid(),s.getsockname()[1]]),encoding='utf-8',newline='\\n')\ntime.sleep(60)\n",
            encoding="utf-8", newline="\n")
        self.workspace.joinpath("marker.py").write_text(
            "import os,sys,subprocess,time\nfrom pathlib import Path\nPath('parent.pid').write_bytes(str(os.getpid()).encode())\nsubprocess.Popen([sys.executable,'child.py'])\ntime.sleep(60)\n",
            encoding="utf-8", newline="\n")
        server = AcceptanceServer(self.config, self.root, "pending")
        with server:
            run_id = server.start_run(self.workspace)
            approval = self.pending(server, run_id)["approval"]
            self.approve(server, run_id, approval).raise_for_status()
            end = time.monotonic() + 8
            while not self.workspace.joinpath("child.json").exists():
                if time.monotonic() > end:
                    self.fail("child did not start")
                time.sleep(0.02)
            child, port = json.loads(self.workspace.joinpath("child.json").read_text())
            parent = int(self.workspace.joinpath("parent.pid").read_text())
            self.assertTrue(process_alive(parent))
            self.assertTrue(process_alive(child))
            server.stop(hard=True)
        self.assert_cleanup(server)
        end = time.monotonic() + 5
        while process_alive(parent) or process_alive(child):
            if time.monotonic() > end:
                self.fail("hard exit left a running Job process")
            time.sleep(0.02)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
        with AcceptanceServer(self.config, self.root, "pending") as restarted:
            self.assertEqual(restarted.snapshot(run_id)["status"], "interrupted")
            self.assertEqual(self.approve(restarted, run_id, approval).status_code, 409)
        self.assert_cleanup(restarted)

    def test_real_http_shell_timeout_and_model_cancel(self):
        self.workspace.joinpath("marker.py").write_text(
            "import os,time\nfrom pathlib import Path\nPath('parent.pid').write_bytes(str(os.getpid()).encode())\ntime.sleep(60)\n",
            encoding="utf-8", newline="\n")
        server = AcceptanceServer(self.config, self.root, "timeout")
        with server:
            run_id = server.start_run(self.workspace)
            self.approve(server, run_id, self.pending(server, run_id)["approval"]).raise_for_status()
            snapshot = server.wait(run_id)
            events = remote_events(server, run_id)
            self.assertEqual(snapshot["status"], "unverified")
            self.assertIn("shell_timeout", json.dumps(events))
            self.assertTrue(any(e["type"] == "shell_process_stopped" for e in events))
            if self.workspace.joinpath("parent.pid").exists():
                self.assertFalse(process_alive(int(self.workspace.joinpath("parent.pid").read_text())))
        self.assert_cleanup(server)
        server = AcceptanceServer(self.config, self.root, "slow_model")
        with server:
            run_id = server.start_run(self.workspace)
            server.client.post(f"/api/runs/{run_id}/cancel", json={}).raise_for_status()
            self.assertEqual(server.wait(run_id)["status"], "cancelled")
        self.assert_cleanup(server)

    def test_memory_audit_requires_persisted_facts_in_a_later_decision(self):
        memory = {"sequence": 1, "type": "memory_updated", "data": {"memory": {"findings": [
            {"kind": "fact", "source": "input.txt", "text": "47 + 58 = 105; 105 * 3 = 315"}]}}}
        request = {"sequence": 2, "type": "model_requested", "data": {"messages": [
            {"role": "system", "content": "Current plan and task memory: input.txt 105 315"}]}}
        target = {"sequence": 3, "type": "tool_requested", "data": {"name": "run_command", "purpose": "task"}}
        self.assertIsNone(memory_decision_evidence([memory]))
        self.assertIsNone(memory_decision_evidence([memory, target]))
        self.assertIsNotNone(memory_decision_evidence([memory, request, target]))
        request["data"]["messages"] = [{"role": "system", "content": "empty task memory"}]
        self.assertIsNone(memory_decision_evidence([memory, request, target]))
