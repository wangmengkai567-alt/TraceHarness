"""Start/stop only acceptance-owned processes and verify the listener is released."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx


ROOT = Path(__file__).resolve().parents[1]
ACTIVE = {"running", "verifying", "awaiting_approval", "cancelling"}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def copy_sample(destination):
    shutil.copytree(ROOT / "examples/stage_six", destination)
    return destination.resolve()


class AcceptanceServer:
    def __init__(self, config, root, scenario="real", env_file=None, port=None):
        self.config, self.scenario = config, scenario
        self.root = Path(root)
        self.control = self.root / ("server-" + uuid4().hex)
        self.control.mkdir(parents=True)
        self.config_path = self.control / "config.json"
        write_json(self.config_path, config)
        if port is None:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
        self.port, self.origin = port, f"http://127.0.0.1:{port}"
        self.env_file = env_file
        self.process = None
        self.client = None
        self.cleanup = None

    def start(self):
        args = [sys.executable, "-m", "tests.stage_six_server", "--config", str(self.config_path),
                "--port", str(self.port), "--control", str(self.control), "--scenario", self.scenario]
        if self.env_file:
            args.extend(["--env-file", str(self.env_file)])
        # Ensure sample's approved 'python verify.py' uses the same active conda interpreter.
        env = dict(os.environ)
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        self.process = subprocess.Popen(args, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT,
                                        creationflags=0x08000000 if os.name == "nt" else 0)
        try:
            end = time.monotonic() + 25
            while not self.control.joinpath("ready.json").is_file():
                if self.process.poll() is not None:
                    raise RuntimeError("acceptance server exited during startup")
                if time.monotonic() > end:
                    raise TimeoutError("acceptance server did not become ready")
                time.sleep(0.05)
            self.client = httpx.Client(base_url=self.origin, timeout=10, trust_env=False)
            self.session()
            return self
        except BaseException:
            self.stop()
            raise

    def session(self):
        session = self.client.get("/api/session").raise_for_status().json()
        self.client.headers.update({"Origin": self.origin, "X-Harness-Token": session["token"]})

    def start_run(self, workspace, task="acceptance task"):
        return self.client.post("/api/runs", json={"workspace": str(workspace), "task": task}).raise_for_status().json()["run_id"]

    def snapshot(self, run_id):
        return self.client.get(f"/api/runs/{run_id}").raise_for_status().json()

    def wait(self, run_id, predicate=None, timeout=30):
        predicate = predicate or (lambda value: value["status"] not in ACTIVE)
        end = time.monotonic() + timeout
        while True:
            value = self.snapshot(run_id)
            if predicate(value):
                return value
            if time.monotonic() >= end:
                raise TimeoutError("acceptance run state not reached")
            time.sleep(0.05)

    def stop(self, hard=False):
        if not self.process or self.cleanup is not None:
            return
        forced = hard
        try:
            if self.process.poll() is None:
                if hard:
                    self.process.kill()
                else:
                    self.control.joinpath("stop").write_bytes(b"stop\n")
                try:
                    self.process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    forced = True
                    self.process.kill()
                    self.process.wait(timeout=10)
            output = self.process.communicate(timeout=5)[0].decode("utf-8", errors="replace")
            self.control.joinpath("server.log").write_text(output.replace("\r\n", "\n").replace("\r", "\n"), encoding="utf-8", newline="\n")
        finally:
            if self.client:
                self.client.close()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", self.port))
        self.cleanup = {"pid": self.process.pid, "exit_code": self.process.returncode,
                        "process_exited": self.process.poll() is not None, "port": self.port,
                        "port_released": True, "hard_stop": forced}
        write_json(self.control / "cleanup.json", self.cleanup)

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()


def disk_events(folder):
    events = []
    for line in folder.joinpath("events.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def remote_events(server, run_id):
    cursor, events = 0, []
    while True:
        page = server.client.get(f"/api/runs/{run_id}/events?after={cursor}").raise_for_status().json()
        events.extend(page["events"])
        cursor = page["cursor"]
        if not page["has_more"]:
            return events
