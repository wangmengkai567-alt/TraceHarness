"""Real model repairs a failed configured check; fixed shell checks use normal Web approvals."""
import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx

from harness.config import load_config
from harness.files import digest
from harness.web import RunManager, create_app
from tests.test_stage_four import command


async def main():
    workspace = Path("runs") / ("stage-five-workspace-" + uuid4().hex)
    workspace.mkdir(parents=True)
    workspace.joinpath("result.txt").write_text("incorrect\n", encoding="utf-8", newline="\n")
    checker = ("from pathlib import Path\nimport sys\n"
               "ok=Path('result.txt').read_bytes()==b'correct: 315\\n'\n"
               "print('CHECK PASS' if ok else 'CHECK FAIL')\nsys.exit(0 if ok else 7)\n")
    workspace.joinpath("check.py").write_text(checker, encoding="utf-8", newline="\n")
    expected = command("check.py") + ("; exit $LASTEXITCODE" if os.name == "nt" else "")
    config = replace(load_config(Path("config.example.json"), Path(".env")), max_turns=12,
                     max_completion_tokens=2048, max_verification_attempts=2, checks=[
                         {"id": "text", "type": "file_contains", "path": "result.txt", "text": "correct: 315"},
                         {"id": "exact_bytes", "type": "shell", "command": expected, "timeout_seconds": 10}])
    manager = RunManager(config, secret_values=tuple(os.environ.get(key, "") for key in ("HARNESS_API_KEY", "TAVILY_API_KEY")))
    approvals = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(manager)), base_url="http://127.0.0.1:8765") as client:
            token = (await client.get("/api/session")).json()["token"]
            client.headers.update({"X-Harness-Token": token, "Origin": "http://127.0.0.1:8765"})
            response = await client.post("/api/runs", json={"workspace": str(workspace.resolve()),
                "task": "这是失败恢复验收，严格按两段完成。第一段：你的第一次回复必须只有一句‘提交初次结果，请执行检查’，不要调用任何工具，不要修正文件，让执行器先得到检查失败。第二段：收到真实检查失败反馈后，建立简短计划，读取 result.txt 的实际哈希，用 write_file 将其改成精确文本 correct: 315 加一个 LF 换行，更新计划，再给最终回复。不得修改 check.py，不调用 run_command，执行器会用配置的命令检查并单独请求用户批准。回复简洁，不声称未经执行的检查通过。"})
            response.raise_for_status()
            run_id = response.json()["run_id"]
            async with asyncio.timeout(config.total_timeout_seconds + 15):
                while True:
                    response = await client.get(f"/api/runs/{run_id}")
                    response.raise_for_status()
                    snapshot = response.json()
                    if snapshot["status"] not in {"running", "verifying", "awaiting_approval", "cancelling"}:
                        break
                    approval = snapshot["approval"]
                    if approval and approval["status"] == "pending":
                        if (approval["command"] != expected or approval["timeout_seconds"] != 10
                                or approval["workspace"] != str(workspace.resolve()) or len(approvals) >= 2):
                            raise AssertionError("unexpected command; not approved")
                        approvals.append(dict(approval))
                        (await client.post(f"/api/runs/{run_id}/approvals/{approval['id']}", json={"decision": "approve"})).raise_for_status()
                    await asyncio.sleep(0.1)
            events = [json.loads(line) for line in manager.active.store.directory.joinpath("events.jsonl").read_text(encoding="utf-8").splitlines()]
            verifications = [e["data"] for e in events if e["type"] == "verification_finished"]
            checks = {
                "failed_then_passed": [v["passed"] for v in verifications] == [False, True],
                "independent_shell_approvals": len(approvals) == 2 and approvals[0]["id"] != approvals[1]["id"],
                "model_received_failed_evidence": any(e["type"] == "model_requested" and "Executor verification failed" in json.dumps(e["data"]["messages"]) for e in events),
                "one_successful_repair_write": sum(e["type"] == "tool_responded" and e["data"]["name"] == "write_file" and e["data"]["result"]["ok"] for e in events) == 1,
                "exact_artifact": workspace.joinpath("result.txt").read_bytes() == b"correct: 315\n",
                "checker_unchanged": digest(workspace.joinpath("check.py").read_bytes()) == digest(checker.encode()),
                "completed_only_after_checks": snapshot["status"] == "completed" and snapshot["result"]["verified"],
                "shell_trees_stopped": sum(e["type"] == "shell_process_stopped" for e in events) == 2,
                "report_has_evidence_and_artifacts": bool(snapshot["result"]["checks"] and snapshot["result"]["artifacts"]),
            }
            report = {"run_id": run_id, "model": config.model, "config": config.snapshot(),
                      "finished_at": snapshot["result"]["finished_at"], "workspace": str(workspace.resolve()),
                      "approvals": approvals, "verification_attempts": verifications,
                      "result": snapshot["result"], "checks": checks, "passed": all(checks.values())}
            Path("docs/stage-five-live.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
            print(json.dumps({"run_id": run_id, "checks": checks, "passed": report["passed"]}, ensure_ascii=False))
            return 0 if report["passed"] else 1
    finally:
        await manager.shutdown()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
