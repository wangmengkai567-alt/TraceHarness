"""Real-model regression through in-process Web API; starts no listening server."""
import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx

from harness.config import load_config
from harness.models import OpenAICompatibleModel, parse_reply
from harness.web import RunManager, create_app


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-run", help="Replay safe historical responses before the real final response")
    args = parser.parse_args()
    config = load_config(Path("config.example.json"), Path(".env"))
    secret_values = tuple(os.environ.get(key, "") for key in ("HARNESS_API_KEY", "TAVILY_API_KEY"))
    real = OpenAICompatibleModel(config, os.environ.get("HARNESS_API_KEY", ""))
    prefix = []
    if args.replay_run:
        if len(args.replay_run) != 32 or any(c not in "0123456789abcdef" for c in args.replay_run):
            raise ValueError("invalid run id")
        folder = Path(config.runs_dir) / args.replay_run
        events = [json.loads(line) for line in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        prefix = [e["data"]["message"] for e in events if e["type"] == "model_responded"][:config.max_turns - 1]
        allowed = {"update_plan", "update_memory", "read_file", "list_files", "add_integers", "multiply_integers"}
        if any(call["function"]["name"] not in allowed for message in prefix for call in message.get("tool_calls", [])):
            raise ValueError("historical prefix contains an operation outside the safe replay set")

    reports = []
    scenarios = [False, True] if prefix else [False]
    for replay in scenarios:
        class Model:
            def __init__(self):
                self.index = 0
                self.real_requests = 0

            async def complete(self, messages, tools):
                if replay and self.index < len(prefix):
                    message = prefix[self.index]
                    self.index += 1
                    return parse_reply({"choices": [{"message": message,
                        "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}]}, config)
                self.real_requests += 1
                return await real.complete(messages, tools)

        model = Model()
        manager = RunManager(config, lambda: model, secret_values)
        app = create_app(manager, 18766)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                          base_url="http://127.0.0.1:18766") as client:
                session = (await client.get("/api/session")).json()
                client.headers.update({"X-Harness-Token": session["token"], "Origin": "http://127.0.0.1:18766"})
                response = await client.post("/api/runs", json={"task": "agent是什么？", "workspace": str(Path.cwd()), "mode": "model"})
                response.raise_for_status()
                run_id = response.json()["run_id"]
                await asyncio.wait_for(asyncio.shield(manager.active.job), config.total_timeout_seconds + 5)
                snapshot = (await client.get(f"/api/runs/{run_id}")).raise_for_status().json()
                result = snapshot["result"]
                checks = {"final_answer_present": result["status"] == "unverified" and bool(result["answer"]),
                          "within_turn_limit": result["turns"] <= config.max_turns,
                          "no_file_artifacts": not snapshot["artifacts"]}
                if replay:
                    checks["last_turn_final_response"] = result["turns"] == config.max_turns and model.real_requests == 1
                else:
                    checks["general_question_without_tools"] = result["tool_calls"] == 0
                reports.append({"scenario": "historical_prefix_plus_real_final_response" if replay else "real_general_question",
                                "run_id": run_id, "status": result["status"], "reason": result["reason"],
                                "turns": result["turns"], "real_requests": model.real_requests, "answer": result["answer"],
                                "checks": checks, "passed": all(checks.values())})
        finally:
            await manager.shutdown()
    report = {"model": config.model, "config": config.snapshot(), "historical_run": args.replay_run,
              "listening_server_started": False, "scenarios": reports, "passed": all(r["passed"] for r in reports)}
    Path("docs/turn-limit-live.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8", newline="\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
