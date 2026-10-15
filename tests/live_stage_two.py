"""Explicit real-provider acceptance: python -m tests.live_stage_two (uses local .env)."""
import asyncio
import hashlib
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

from harness.config import load_config
from harness.models import OpenAICompatibleModel
from harness.runner import Runner


async def main():
    config = replace(load_config(env_file=Path(".env")), max_turns=20, max_model_calls=24,
                     max_completion_tokens=4096, total_timeout_seconds=360,
                     request_timeout_seconds=60,
                     recent_history_turns=2)
    # Persistent, inspectable scratch workspace; never overwrite the sample inputs.
    from uuid import uuid4
    workspace = Path("runs") / ("stage-two-workspace-" + uuid4().hex)
    shutil.copytree(Path("examples/stage_two"), workspace)
    secret = os.environ.get("HARNESS_API_KEY", "")
    result = await Runner(config, OpenAICompatibleModel(config, secret), secrets=(secret,)).run(
        (workspace / "task.txt").read_text(encoding="utf-8") + "\n计划最多三个步骤，记忆与最终回复保持简洁。", workspace)
    return report_evidence(config, result, workspace)


def report_evidence(config, result, workspace):
    directory = Path(result["run_directory"])
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    output = workspace / "summary.txt"
    raw = output.read_bytes() if output.exists() else b""
    text = raw.decode("utf-8-sig")
    memory_events = [e for e in events if e["type"] == "memory_updated"
                     and any(f.get("kind") == "fact" and "42" in f.get("text", "")
                             and "input.txt" in f.get("source", "")
                             for f in e["data"]["memory"]["findings"])]
    memory_used = any(e["type"] == "model_requested" and e["sequence"] > saved["sequence"]
                      and any(m["role"] == "system" and "Current plan and task memory" in m["content"]
                              and "42" in m["content"] for m in e["data"]["messages"])
                      for saved in memory_events for e in events)
    checks = {"terminal_unverified": result["status"] == "unverified",
              "artifact_rules": text.startswith("资料摘要") and "42" in text and "来源：input.txt" in text and b"\r" not in raw,
              "plan_recorded": sum(e["type"] == "plan_updated" for e in events) >= 2,
              "plan_completed": bool((result.get("state") or {}).get("plan"))
                                and all(step["status"] == "completed" for step in result["state"]["plan"]),
              "memory_in_later_request": memory_used,
              "artifact_read_back": any(e["type"] == "tool_responded" and e["data"]["name"] == "read_file"
                                        and e["data"]["result"]["ok"]
                                        and isinstance(e["data"]["result"]["data"], dict)
                                        and e["data"]["result"]["data"].get("path") == "summary.txt"
                                        and e["data"]["result"]["data"].get("sha256") == hashlib.sha256(raw).hexdigest()
                                        for e in events)}
    report = {"provider": config.provider, "model": config.model, "run_id": result["run_id"],
              "finished_at": result["finished_at"], "config": config.snapshot(),
              "status": result["status"], "reason": result["reason"], "checks": checks,
              "workspace": str(workspace), "run_directory": str(directory), "passed": all(checks.values())}
    with Path("docs/stage-two-live.json").open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
