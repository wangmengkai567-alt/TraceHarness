import asyncio
import ctypes
import json
import os
import shlex
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from harness.config import Config
from harness.contracts import ModelReply, ToolCall
from harness.processes import TerminationUnconfirmed
from harness.shell import ShellTools
from harness.web import APIError, RunManager
from tests.test_runner import ScriptModel


def command(script):
    if os.name == "nt":
        return "& '" + sys.executable.replace("'", "''") + "' '" + script + "'"
    return shlex.join([sys.executable, script])


def shell_call(identifier, script="work.py", timeout=5):
    return ModelReply(tool_calls=(ToolCall(identifier, "run_command", json.dumps({
        "command": command(script), "timeout_seconds": timeout})),), finish_reason="tool_calls")


def alive(pid):
    if os.name == "nt":
        from ctypes import wintypes as w
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes, k.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        k.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        k.CloseHandle.argtypes = [w.HANDLE]
        handle = k.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = w.DWORD()
            if not k.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return code.value == 259
        finally:
            k.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class StageFourTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.config = Config(model="test", base_url="https://test.invalid/v1",
                             runs_dir=str(self.root / "runs"), max_turns=6)
        self.manager = None
        self.write("work.py", "from pathlib import Path\np=Path('marker'); p.write_bytes(p.read_bytes()+b'x' if p.exists() else b'x')\nprint('ok')\n")

    def write(self, name, content):
        (self.workspace / name).write_text(content, encoding="utf-8", newline="\n")

    async def asyncTearDown(self):
        if self.manager:
            await self.manager.shutdown()
        self.tmp.cleanup()

    async def start(self, *replies, approval_timeout=2, config=None):
        model = ScriptModel(*replies, ModelReply(content="done"))
        self.manager = RunManager(config or self.config, lambda: model,
                                  approval_timeout=approval_timeout)
        run_id = (await self.manager.start({"task": "test shell", "workspace": str(self.workspace)}))["run_id"]
        return run_id, model

    async def pending(self, call_id=None):
        async with asyncio.timeout(5):
            while True:
                approval = self.manager.active.approval
                if approval and approval["status"] == "pending" and (not call_id or approval["call_id"] == call_id):
                    return dict(approval)
                await asyncio.sleep(0.01)

    async def decide(self, run_id, approval, decision="approve"):
        return await self.manager.decide(run_id, approval["id"], {"decision": decision})

    async def finish(self):
        await asyncio.wait_for(asyncio.shield(self.manager.active.job), 10)
        return self.manager.snapshot(self.manager.active.store.run_id)

    def events(self):
        return [json.loads(line) for line in (self.manager.active.store.directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]

    async def test_unapproved_then_rejected_never_starts_and_feedback_reaches_model(self):
        run_id, model = await self.start(shell_call("one"))
        approval = await self.pending()
        await asyncio.sleep(0.08)
        self.assertFalse((self.workspace / "marker").exists())
        await self.decide(run_id, approval, "reject")
        await self.finish()
        self.assertFalse((self.workspace / "marker").exists())
        self.assertIn("approval_reject", json.dumps(model.requests[-1]))
        self.assertFalse(any(e["type"] == "shell_process_started" for e in self.events()))

    async def test_approval_is_bound_single_use_and_every_call_needs_new_approval(self):
        run_id, _ = await self.start(shell_call("one"), shell_call("two"))
        first = await self.pending("one")
        self.assertEqual(first["command"], command("work.py"))
        self.assertEqual(first["argv"][-1], first["command"])
        self.assertEqual(first["workspace"], str(self.workspace.resolve()))
        self.assertEqual(first["timeout_seconds"], 5)
        with self.assertRaises(APIError):
            await self.manager.decide(run_id, first["id"], {"decision": "approve", "command": "changed"})
        await self.decide(run_id, first)
        with self.assertRaises(APIError):
            await self.decide(run_id, first)
        second = await self.pending("two")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual((self.workspace / "marker").read_bytes(), b"x")
        with self.assertRaises(APIError):
            await self.decide(run_id, first)
        await self.decide(run_id, second)
        await self.finish()
        self.assertEqual((self.workspace / "marker").read_bytes(), b"xx")
        with self.assertRaises(APIError):
            await self.decide(run_id, second)

    async def test_expired_pending_without_browser_never_executes(self):
        run_id, _ = await self.start(shell_call("one"), approval_timeout=0.1)
        approval = await self.pending()
        await self.finish()
        with self.assertRaises(APIError):
            await self.decide(run_id, approval)
        self.assertFalse((self.workspace / "marker").exists())
        self.assertTrue(any(e["type"] == "approval_expired" for e in self.events()))

    async def test_cancel_pending_invalidates_approval(self):
        run_id, _ = await self.start(shell_call("one"))
        approval = await self.pending()
        await self.manager.cancel(run_id)
        with self.assertRaises(APIError):
            await self.decide(run_id, approval)
        self.assertEqual((await self.finish())["status"], "cancelled")
        self.assertFalse((self.workspace / "marker").exists())

    async def test_exit_code_output_and_bounded_drain(self):
        self.write("fail.py", "import sys\nprint('x'*1000000)\nprint('error', file=sys.stderr)\nsys.exit(7)\n")
        cmd = command("fail.py") + ("; exit $LASTEXITCODE" if os.name == "nt" else "")
        reply = ModelReply(tool_calls=(ToolCall("one", "run_command", json.dumps({
            "command": cmd, "timeout_seconds": 5})),), finish_reason="tool_calls")
        run_id, _ = await self.start(reply)
        await self.decide(run_id, await self.pending())
        await self.finish()
        result = next(e["data"]["result"] for e in self.events() if e["type"] == "tool_responded")
        self.assertFalse(result["ok"])
        self.assertEqual(result["data"]["exit_code"], 7)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["data"]["output"]), self.config.max_output_chars // 2)
        self.assertGreater(result["data"]["output_bytes"], 1000000)

    async def tree(self, mode):
        self.write("child.py", "import os,time,socket\nfrom pathlib import Path\ns=socket.socket();s.bind(('127.0.0.1',0));s.listen()\nPath('child.json').write_text(__import__('json').dumps([os.getpid(),s.getsockname()[1]]),encoding='utf-8',newline='\\n')\ntime.sleep(60)\n")
        self.write("tree.py", "import os,subprocess,sys,time\nfrom pathlib import Path\nPath('parent.pid').write_bytes(str(os.getpid()).encode())\nsubprocess.Popen([sys.executable,'child.py'])\ntime.sleep(60)\n")
        config = replace(self.config, total_timeout_seconds=3) if mode == "budget" else self.config
        run_id, _ = await self.start(shell_call("one", "tree.py", 2 if mode == "timeout" else 30), config=config)
        await self.decide(run_id, await self.pending())
        async with asyncio.timeout(5):
            while not (self.workspace / "child.json").exists():
                await asyncio.sleep(0.02)
        child, port = json.loads((self.workspace / "child.json").read_text())
        parent = int((self.workspace / "parent.pid").read_text())
        self.assertTrue(alive(parent))
        self.assertTrue(alive(child))
        if mode == "cancel":
            await self.manager.cancel(run_id)
        elif mode == "shutdown":
            await self.manager.shutdown()
        snapshot = await self.finish()
        self.assertFalse(alive(parent), "parent survived")
        self.assertFalse(alive(child), "child survived")
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
        stopped = [e for e in self.events() if e["type"] == "shell_process_stopped"]
        self.assertEqual(len(stopped), 1)
        if mode in {"cancel", "shutdown"}:
            self.assertEqual(snapshot["status"], "cancelled")
        elif mode == "budget":
            self.assertEqual(snapshot["status"], "budget_exceeded")
        else:
            self.assertTrue(any(e["data"].get("result", {}).get("error") == "shell_timeout" for e in self.events()))

    async def test_windows_tree_timeout_without_browser(self):
        await self.tree("timeout")

    async def test_windows_tree_cancel(self):
        await self.tree("cancel")

    async def test_windows_tree_total_budget(self):
        await self.tree("budget")

    async def test_windows_tree_server_shutdown(self):
        await self.tree("shutdown")

    async def test_unconfirmed_termination_blocks_following_command_and_new_run(self):
        with patch("harness.shell.run_process", side_effect=TerminationUnconfirmed):
            run_id, _ = await self.start(shell_call("one"), shell_call("two"))
            await self.decide(run_id, await self.pending())
            snapshot = await self.finish()
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["result"]["reason"], "process_termination_unconfirmed")
        self.assertEqual(sum(e["type"] == "approval_requested" for e in self.events()), 1)
        with self.assertRaises(APIError):
            await self.manager.start({"task": "second", "workspace": str(self.workspace)})

    async def test_invalid_parameters_do_not_reach_approval(self):
        async def approve(spec):
            self.fail("invalid call requested approval")
        tools = ShellTools(self.workspace, self.config, approve, lambda *a, **k: None, asyncio.Event())
        for args in ({"command": "x", "timeout_seconds": True}, {"command": "x", "timeout_seconds": 61},
                     {"command": "x", "timeout_seconds": float("nan")}, {"command": ""},
                     {"command": "x", "timeout_seconds": 1, "workspace": ".."}):
            result = await tools.execute(ToolCall("one", "run_command", json.dumps(args)))
            self.assertEqual(result.error, "invalid_shell_arguments")

    async def test_cancel_cleanup_failure_is_failed_instead_of_cancelled(self):
        async def uncertain(*args):
            try:
                await asyncio.sleep(60)
            finally:
                raise TerminationUnconfirmed()
        with patch("harness.shell.run_process", side_effect=uncertain):
            run_id, _ = await self.start(shell_call("one"))
            await self.decide(run_id, await self.pending())
            await asyncio.sleep(0.05)
            await self.manager.cancel(run_id)
            snapshot = await self.finish()
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["result"]["reason"], "process_termination_unconfirmed")
        self.assertTrue(self.manager.process_control_blocked)
