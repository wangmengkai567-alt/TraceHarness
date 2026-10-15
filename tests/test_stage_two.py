import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from harness.config import Config
from harness.contracts import ModelReply, ToolCall
from harness.files import FileTools, digest
from harness.runner import Runner
from tests.test_runner import ScriptModel


def action(name, args, id):
    return ModelReply(tool_calls=(ToolCall(str(id), name, json.dumps(args)),), finish_reason="tool_calls")


def plan(status="in_progress"):
    return {"steps": [{"id": "summary", "goal": "Write sourced summary", "status": status,
                       "completion_condition": "summary.txt contains source and 42"}], "reason": "advance"}


class StageTwoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        self.files = FileTools(self.root, max_bytes=100, max_entries=2)
        self.config = Config(model="test", base_url="https://test.invalid/v1",
                             runs_dir=str(Path(self.tmp.name) / "runs"), max_turns=16,
                             recent_history_turns=1)

    async def execute(self, name, **args):
        return await self.files.execute(ToolCall("id", name, json.dumps(args)))

    async def test_files_conflict_encoding_and_creation(self):
        result = await self.execute("write_file", path="new.txt", content="\ufeffnew\r\n", expected_hash=None)
        self.assertTrue(result.ok)
        self.assertEqual((self.root / "new.txt").read_bytes(), b"new\n")
        result = await self.execute("write_file", path="new.txt", content="oops", expected_hash=None)
        self.assertEqual(result.error, "file_conflict")
        path = self.root / "bom.txt"
        path.write_bytes(b"\xef\xbb\xbfhello\r\n")
        read = await self.execute("read_file", path="bom.txt")
        path.write_bytes(b"external\n")
        result = await self.execute("write_file", path="bom.txt", content="oops", expected_hash=read.data["sha256"])
        self.assertEqual(result.error, "file_conflict")
        self.assertEqual(path.read_bytes(), b"external\n")
        path.write_bytes(b"\xef\xbb\xbfhello\r\n")
        result = await self.execute("write_file", path="bom.txt", content="updated\n", expected_hash=digest(path.read_bytes()))
        self.assertTrue(result.ok)
        self.assertEqual(path.read_bytes(), b"\xef\xbb\xbfupdated\r\n")

    async def test_boundaries_and_invalid_content(self):
        for path in ("../outside", str(Path(self.tmp.name) / "outside"), "C:foo", "a:stream", "CON", "a."):
            self.assertFalse((await self.execute("write_file", path=path, content="x", expected_hash=None)).ok)
        self.assertEqual((await self.execute("write_file", path="AGENTS.md", content="x", expected_hash=None)).error,
                         "rules_are_read_only")
        for raw, error in ((b"x" * 101, "file_too_large"), (b"a\x00b", "binary_file"),
                           (b"\xff", "unsupported_encoding")):
            (self.root / "bad").write_bytes(raw)
            self.assertEqual((await self.execute("read_file", path="bad")).error, error)
        self.assertEqual((await self.execute("write_file", path="large", content="x" * 101, expected_hash=None)).error,
                         "file_too_large")
        for i in range(3):
            (self.root / str(i)).write_bytes(b"x")
        result = await self.execute("list_files", path=".")
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.data["entries"]), 2)

    async def test_external_links_and_hardlinks(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"secret")
        link = self.root / "link"
        if os.name == "nt":
            completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            self.assertEqual(completed.returncode, 0)
        else:
            link.symlink_to(outside, target_is_directory=True)
        for name, args in (("read_file", {"path": "link/secret"}), ("list_files", {"path": "link"}),
                           ("write_file", {"path": "link/new", "content": "x", "expected_hash": None})):
            self.assertEqual((await self.execute(name, **args)).error, "path_outside_workspace")
        hard = self.root / "hard"
        os.link(outside / "secret", hard)
        self.assertEqual((await self.execute("write_file", path="hard", content="x", expected_hash=digest(b"secret"))).error,
                         "write_link_rejected")
        self.assertEqual((outside / "secret").read_bytes(), b"secret")

    async def test_plan_memory_history_and_rule_snapshot(self):
        (self.root / "AGENTS.md").write_bytes(b"Always include Source: input.txt")
        (self.root / "input.txt").write_bytes(b"Observed value: 42")
        memory = {"goal": "summary", "constraints": ["Include Source"], "progress": ["read input"],
                  "findings": [{"text": "42", "kind": "fact", "source": "input.txt"},
                               {"text": "may be useful", "kind": "hypothesis", "source": "inference"}],
                  "artifacts": ["summary.txt"], "unresolved": []}
        outer = self

        class Model(ScriptModel):
            async def complete(self, messages, tools):
                if len(self.requests) == 2:
                    (outer.root / "AGENTS.md").write_bytes(b"changed rule")
                if len(self.requests) == 4:
                    # The read response has expired, but findings and original rules remain.
                    wire = json.dumps(messages)
                    outer.assertIn('42', wire)
                    outer.assertIn('Always include Source', wire)
                    outer.assertNotIn('changed rule', wire)
                    outer.assertFalse(any('Observed value' in (m.get("content") or "") for m in messages))
                return await super().complete(messages, tools)

        model = Model(action("update_plan", plan(), 1), action("read_file", {"path": "input.txt"}, 2),
                      action("update_memory", memory, 3), action("update_plan", plan(), 4),
                      action("write_file", {"path": "summary.txt", "content": "42\nSource: input.txt\n", "expected_hash": None}, 5),
                      action("update_plan", plan("completed"), 6), ModelReply(content="done"))
        result = await Runner(self.config, model).run("summary", self.root)
        self.assertEqual(result["status"], "unverified")
        self.assertEqual((self.root / "summary.txt").read_bytes(), b"42\nSource: input.txt\n")
        directory = Path(result["run_directory"])
        events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
        self.assertEqual(sum(e["type"] == "plan_updated" for e in events), 3)
        self.assertTrue(any(e["type"] == "context_history_limited" for e in events))
        self.assertEqual(json.loads((directory / "state.json").read_text())["memory"], memory)
        self.assertEqual(json.loads((directory / "rules.json").read_text())["content"], "Always include Source: input.txt")
        next_model = ScriptModel(ModelReply(content="done"))
        await Runner(self.config, next_model).run("next", self.root)
        self.assertIn("changed rule", json.dumps(next_model.requests[0]))
        for path in directory.iterdir():
            self.assertNotIn(b"\r", path.read_bytes())

    async def test_missing_oversized_rules_and_fixed_context_budget(self):
        model = ScriptModel(ModelReply(content="done"))
        result = await Runner(self.config, model).run("task", self.root)
        self.assertEqual(result["status"], "unverified")
        (self.root / "AGENTS.md").write_bytes(b"x" * 20)
        model = ScriptModel()
        result = await Runner(replace(self.config, max_rules_bytes=10), model).run("task", self.root)
        self.assertEqual(result["reason"], "rules_file_too_large")
        self.assertFalse(model.requests)
        model = ScriptModel()
        result = await Runner(replace(self.config, max_input_chars=100), model).run("task", self.root)
        self.assertEqual(result["reason"], "input_char_limit")
        self.assertFalse(model.requests)

    async def test_plan_required_and_invalid_state_has_no_effect(self):
        model = ScriptModel(action("write_file", {"path": "oops", "content": "x", "expected_hash": None}, 1),
                            action("update_plan", {"steps": [], "reason": "bad"}, 2), ModelReply(content="done"))
        result = await Runner(self.config, model).run("task", self.root)
        self.assertFalse((self.root / "oops").exists())
        self.assertIn("plan_required", json.dumps(model.requests[1]))
        self.assertEqual(result["state"]["plan"], [])
