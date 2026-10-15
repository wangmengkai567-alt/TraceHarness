import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("HARNESS_") and key != "TAVILY_API_KEY"}
        # Keep temporary runtime data isolated without requiring a project installation.
        root = str(Path(__file__).resolve().parents[1])
        existing = self.env.get("PYTHONPATH")
        self.env["PYTHONPATH"] = root + (os.pathsep + existing if existing else "")

    def command(self, *args):
        return subprocess.run([sys.executable, "-m", "harness", *args], cwd=self.folder,
                              env=self.env, capture_output=True, text=True, encoding="utf-8", timeout=10)

    def test_run_requires_real_model_configuration(self):
        run = self.command("run", "--task", "calculate 47+58")
        self.assertEqual(run.returncode, 2)
        self.assertIn("HARNESS_MODEL", run.stdout)
        self.assertFalse((self.folder / "runs").exists())

    def test_live_check_requires_real_model_key(self):
        config = self.folder / "config.json"
        config.write_text(json.dumps({"provider": "openai_compatible", "model": "local-model",
                                      "base_url": "http://127.0.0.1:1234/v1"}), encoding="utf-8")
        run = self.command("live-check", "--config", str(config))
        self.assertEqual(run.returncode, 2)
        self.assertIn("HARNESS_API_KEY", run.stdout)
        self.assertFalse((self.folder / "runs").exists())

    def test_inspect_missing_terminal_and_invalid_run_id(self):
        run_id = "a" * 32
        folder = self.folder / "runs" / run_id
        folder.mkdir(parents=True)
        (folder / "metadata.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
        result = self.command("inspect", run_id)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["status"], "no_terminal_result")
        self.assertEqual(self.command("inspect", "../escape").returncode, 2)


if __name__ == "__main__":
    unittest.main()
