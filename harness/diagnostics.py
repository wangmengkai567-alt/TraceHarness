"""显式运行的模型工具调用诊断：固定场景、执行入口与事件证据检查。

普通任务不依赖这里的场景；诊断通过只说明工具调用链可用，不代表通用任务正确。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .arithmetic import ArithmeticTools
from .config import Config
from .models import Model
from .runner import Runner


LIVE_CHECK_TASK = "先调用 add_integers 计算 47+58，再把工具返回值交给 multiply_integers 乘以 3，报告最终结果。"


async def run_live_check(config: Config, model: Model, workspace: Path,
                        cancel: asyncio.Event | None = None, *, secrets: tuple[str, ...] = ()) -> dict:
    """通过正常执行链运行诊断，显式提供场景所需的计算工具。"""
    return await Runner(config, model, tools=ArithmeticTools(), secrets=secrets).run(
        LIVE_CHECK_TASK, workspace, cancel)


def check_live_evidence(result: dict) -> bool:
    """检查模型是否按顺序调用计算工具、读到反馈并在最终回答中使用结果。"""
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
    # 不能只检查最终答案含 315：还需证明模型先读到 105，再请求乘法并读到 315。
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
