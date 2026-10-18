"""命令行入口：运行任务、检查真实模型工具链、查看记录或启动本地 Web。

调用链为 main → execute → Runner.run；live-check 委托 diagnostics 运行场景和检查证据。
serve 子命令转入 web.serve。
CLI 运行任务时没有审批代理，因此不开放 Shell，命令审批需通过 Web 完成。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from . import diagnostics
from .config import load_config
from .models import OpenAICompatibleModel
from .runner import Runner
from .store import RUN_ID
from .processes import process_alive


def print_json(value) -> None:
    # 终端输出统一为便于阅读的 JSON，中文不转义为 Unicode 编码。
    print(json.dumps(value, ensure_ascii=False, indent=2))


async def execute(args, config, *, live: bool = False) -> dict:
    # 收集密钥供日志脱敏；Ctrl+C 转成取消事件，让 Runner 有机会收尾并保存结果。
    secrets = tuple(os.environ.get(name, "") for name in ("HARNESS_API_KEY", "TAVILY_API_KEY"))
    model = OpenAICompatibleModel(config, os.environ.get("HARNESS_API_KEY", ""))
    cancel = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(cancel.set))
    try:
        if live:
            result = await diagnostics.run_live_check(config, model, Path(args.workspace), cancel,
                                                      secrets=secrets)
        else:
            result = await Runner(config, model, secrets=secrets).run(args.task, Path(args.workspace), cancel)
    finally:
        # 还原信号处理函数，避免影响嵌入调用或后续命令。
        signal.signal(signal.SIGINT, previous)
    return result


def parser() -> argparse.ArgumentParser:
    # 在这里集中声明子命令及参数；解析参数本身不会加载配置或启动服务。
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
    # 退出码：0 为正常结束，1 为执行/验收失败，2 为配置或 IO 错误，130 为取消。
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
            # inspect 只读取历史记录，不恢复任务、不重放命令，也不复用旧审批。
            if not RUN_ID.fullmatch(args.run_id):
                raise ValueError("run_id must be a date + daily sequence ID or a legacy UUID")
            folder = args.runs_dir.resolve() / args.run_id
            result = folder / "result.json"
            if result.is_file():
                print_json(json.loads(result.read_text(encoding="utf-8")))
            else:
                metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
                running = False
                # 没有终态文件时，PID 存活仅作为运行中提示，不能证明仍是原来的任务。
                if type(metadata.get("owner_pid")) is int and metadata["owner_pid"] > 0:
                    running = process_alive(metadata["owner_pid"])
                print_json({"run_id": metadata["run_id"], "status": "running" if running else "interrupted",
                            "note": "No terminal result. Never resume or replay commands; owner PID liveness is only a hint."})
            if args.events:
                print((folder / "events.jsonl").read_text(encoding="utf-8"), end="")
            return 0
        live = args.command == "live-check"
        config = load_config(args.config, args.env_file)
        result = asyncio.run(execute(args, config, live=live))
        print_json(result)
        if live:
            passed = diagnostics.check_live_evidence(result)
            print_json({"live_check_passed": passed,
                        "note": "This checks the real-model tool loop, not general task correctness."})
            return 0 if passed else 1
        return 0 if result["status"] in {"completed", "unverified"} else (130 if result["status"] == "cancelled" else 1)
    except (ValueError, TypeError, OSError) as error:
        # 配置错误仅显示经过脱敏的校验信息；IO 异常使用固定文本，避免泄漏底层细节。
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
