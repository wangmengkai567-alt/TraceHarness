from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
from pathlib import Path

from .config import load_config
from .models import OpenAICompatibleModel
from .runner import LIVE_CHECK_TASK, Runner


def print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


async def execute(args, config, *, live: bool = False) -> dict:
    secrets = tuple(os.environ.get(name, "") for name in ("HARNESS_API_KEY", "TAVILY_API_KEY"))
    model = OpenAICompatibleModel(config, os.environ.get("HARNESS_API_KEY", ""))
    cancel = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(cancel.set))
    try:
        result = await Runner(config, model, secrets=secrets).run(
            LIVE_CHECK_TASK if live else args.task, Path(args.workspace), cancel)
    finally:
        signal.signal(signal.SIGINT, previous)
    return result


def check_live_evidence(result: dict) -> bool:
    """Check tool feedback, subsequent requests, and final answer; not merely a chat."""
    events = [json.loads(line) for line in
              (Path(result["run_directory"]) / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    lookups = [e for e in events if e["type"] == "tool_responded"
               and e["data"]["name"] == "add_integers" and e["data"]["result"]["data"] == {"value": 105}]
    totals = [e for e in events if e["type"] == "tool_responded"
              and e["data"]["name"] == "multiply_integers"
              and e["data"]["result"]["data"] == {"value": 315}]
    if result["status"] != "unverified" or not lookups or not totals:
        return False
    if lookups[0]["sequence"] >= totals[-1]["sequence"]:
        return False
    total_requested = next(e for e in events if e["type"] == "tool_requested"
                           and e["data"]["call_id"] == totals[-1]["data"]["call_id"])
    dependent_request = any(e["type"] == "model_requested"
                            and lookups[0]["sequence"] < e["sequence"] < total_requested["sequence"]
                            and any(m["role"] == "tool" and '105' in m["content"]
                                    for m in e["data"]["messages"])
                            for e in events)
    feedback_seen = any(e["type"] == "model_requested"
                        and e["sequence"] > totals[-1]["sequence"]
                        and any(m["role"] == "tool" and '315' in m["content"]
                                for m in e["data"]["messages"])
                        for e in events)
    answer = result.get("answer") or ""
    return dependent_request and feedback_seen and "315" in answer


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Local agent harness with Web, rules, plans, memory and bounded files (no shell)")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("run", "live-check"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path)
        command.add_argument("--env-file", type=Path, help="Explicit .env path; existing environment wins")
        command.add_argument("--workspace", default=".")
        if name == "run":
            command.add_argument("--task", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("run_id")
    inspect.add_argument("--runs-dir", type=Path, default=Path("runs"))
    inspect.add_argument("--events", action="store_true")
    web = commands.add_parser("serve", help="Start loopback-only Web task UI")
    web.add_argument("--config", type=Path)
    web.add_argument("--env-file", type=Path)
    web.add_argument("--port", type=int, default=8765)
    return root


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        if args.command == "serve":
            from .web import serve
            if not 1 <= args.port <= 65535:
                raise ValueError("port must be between 1 and 65535")
            asyncio.run(serve(load_config(args.config, args.env_file), args.port))
            return 0
        if args.command == "inspect":
            if not re.fullmatch(r"[0-9a-f]{32}", args.run_id):
                raise ValueError("run_id must be the 32-character ID produced by harness")
            folder = args.runs_dir.resolve() / args.run_id
            result = folder / "result.json"
            if result.is_file():
                print_json(json.loads(result.read_text(encoding="utf-8")))
            else:
                metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
                print_json({"run_id": metadata["run_id"], "status": "no_terminal_result",
                            "note": "Run may still be active or interrupted; stage one does not resume it."})
            if args.events:
                print((folder / "events.jsonl").read_text(encoding="utf-8"), end="")
            return 0
        live = args.command == "live-check"
        config = load_config(args.config, args.env_file)
        result = asyncio.run(execute(args, config, live=live))
        print_json(result)
        if live:
            passed = check_live_evidence(result)
            print_json({"live_check_passed": passed,
                        "note": "This checks the real-model tool loop, not general task correctness."})
            return 0 if passed else 1
        return 0 if result["status"] == "unverified" else (130 if result["status"] == "cancelled" else 1)
    except (ValueError, TypeError, OSError) as error:
        # Keep parsing/IO errors safe; selected validation messages are authored here.
        if type(error) is ValueError:
            message = str(error)
            for name in ("HARNESS_API_KEY", "TAVILY_API_KEY"):
                secret = os.environ.get(name, "")
                if secret:
                    message = message.replace(secret, "[REDACTED]")
        else:
            message = "unable to load config/run; check paths and file formats"
        print_json({"error": "configuration_or_io_error", "message": message})
        return 2
    except KeyboardInterrupt:
        return 130
