"""Real model + Web API acceptance; approve only the exact prepared harmless command."""
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx

from harness.config import load_config
from harness.web import RunManager, create_app
from tests.test_stage_four import command


async def main():
    config = replace(load_config(Path("config.example.json"), Path(".env")),
                     max_turns=10, max_completion_tokens=2048)
    workspace = Path("runs") / ("stage-four-workspace-" + uuid4().hex)
    workspace.mkdir(parents=True)
    (workspace / "check.py").write_text(
        "from pathlib import Path\np=Path('proof.txt')\np.write_text('stage-four: 315\\n',encoding='utf-8',newline='\\n')\nprint(p.read_text(encoding='utf-8'),end='')\n",
        encoding="utf-8", newline="\n")
    expected = command("check.py")
    manager = RunManager(config)
    app = create_app(manager)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        session = (await client.get("/api/session")).json()
        client.headers.update({"X-Harness-Token": session["token"], "Origin": "http://127.0.0.1:8765"})
        approvals = []
        try:
            response = await client.post("/api/runs", json={"workspace": str(workspace.resolve()),
                "task": "这是阶段四真实 shell 验收。先建立简短计划，然后仅通过 run_command 执行下述精确命令一次，timeout_seconds=10。不要用文件工具替代执行，不要调用其他命令。根据 shell 实际输出报告结果，标明无通用验证器。命令：\n" + expected})
            response.raise_for_status()
            run_id = response.json()["run_id"]
            async with asyncio.timeout(config.total_timeout_seconds + 15):
                while True:
                    response = await client.get(f"/api/runs/{run_id}")
                    response.raise_for_status()
                    snapshot = response.json()
                    if snapshot["status"] not in {"running", "awaiting_approval", "cancelling"}:
                        break
                    approval = snapshot["approval"]
                    if approval and approval["status"] == "pending":
                        if (approval["command"] != expected or approval["timeout_seconds"] != 10
                                or approval["workspace"] != str(workspace.resolve()) or approvals):
                            raise AssertionError("unexpected command; acceptance refuses approval")
                        approvals.append(approval)
                        decision = await client.post(f"/api/runs/{run_id}/approvals/{approval['id']}", json={"decision": "approve"})
                        decision.raise_for_status()
                    await asyncio.sleep(0.1)
            events = [json.loads(line) for line in (manager.active.store.directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            results = [e["data"]["result"] for e in events if e["type"] == "tool_responded" and e["data"]["name"] == "run_command"]
            checks = {"one_independent_approval": len(approvals) == 1,
                      "real_shell_success": len(results) == 1 and results[0]["ok"] and results[0]["data"]["exit_code"] == 0,
                      "output_used_in_answer": "315" in (snapshot["result"]["answer"] or ""),
                      "artifact_exact": (workspace / "proof.txt").read_bytes() == b"stage-four: 315\n",
                      "tree_cleanup_recorded": any(e["type"] == "shell_process_stopped" and e["data"]["tree_terminated"] for e in events),
                      "unverified_terminal": snapshot["status"] == "unverified"}
            report = {"run_id": run_id, "model": config.model, "config": config.snapshot(),
                      "finished_at": snapshot["result"]["finished_at"], "workspace": str(workspace.resolve()),
                      "approval": approvals, "shell_results": results, "checks": checks, "passed": all(checks.values())}
            Path("docs/stage-four-live.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
            print(json.dumps({"run_id": run_id, "checks": checks, "passed": report["passed"]}, ensure_ascii=False))
            return 0 if report["passed"] else 1
        finally:
            await manager.shutdown()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
