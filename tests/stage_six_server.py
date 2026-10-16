"""Owned acceptance server. Scenario models are test fixtures, never production fallbacks."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

import uvicorn

from harness.config import load_config
from harness.contracts import ModelError, ModelReply
from harness.web import RunManager, create_app
from tests.test_runner import ScriptModel, SlowModel
from tests.test_stage_two import action


def fixture(scenario):
    call = lambda identifier: action("run_command", {"command": "python marker.py", "timeout_seconds": 20}, identifier)
    if scenario == "pending":
        return ScriptModel(call("one"), ModelReply(content="finished"))
    if scenario == "timeout":
        return ScriptModel(action("run_command", {"command": "python marker.py", "timeout_seconds": 1}, "one"), ModelReply(content="timeout reported"))
    if scenario == "request_failure":
        return ScriptModel(ModelError("model_http_401"))
    if scenario == "tool_error":
        return ScriptModel(action("add_integers", {"a": "bad", "b": 1}, "bad"), ModelReply(content="invalid parameters reported"))
    if scenario == "slow_model":
        return SlowModel()
    if scenario == "empty_result":
        return ScriptModel(ModelReply(content="candidate"))
    raise ValueError("unknown fixture scenario")


async def main(args):
    config = load_config(Path(args.config), Path(args.env_file) if args.env_file else None)
    manager = RunManager(config, None if args.scenario == "real" else lambda: fixture(args.scenario),
                         tuple(os.environ.get(key, "") for key in ("HARNESS_API_KEY", "TAVILY_API_KEY")))
    control = Path(args.control)
    server = uvicorn.Server(uvicorn.Config(create_app(manager, args.port), host="127.0.0.1", port=args.port,
                                           access_log=False, proxy_headers=False, log_level="warning"))
    async def stop_signal():
        while not server.started and not server.should_exit:
            await asyncio.sleep(0.02)
        if server.started:
            control.joinpath("ready.json").write_text(json.dumps({"pid": os.getpid(), "port": args.port}) + "\n",
                                                     encoding="utf-8", newline="\n")
        while not control.joinpath("stop").exists() and not server.should_exit:
            await asyncio.sleep(0.05)
        server.should_exit = True
    watcher = asyncio.create_task(stop_signal())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        await manager.shutdown()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stderr.reconfigure(encoding="utf-8", newline="\n")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--env-file")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--control", required=True)
    parser.add_argument("--scenario", default="real", choices=["real", "pending", "timeout", "request_failure", "tool_error", "slow_model", "empty_result"])
    asyncio.run(main(parser.parse_args()))
