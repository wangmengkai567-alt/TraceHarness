"""Real model + Tavily acceptance over an owned Web server; no simulated fallback."""
import argparse
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from dotenv import dotenv_values

from tests.stage_six_support import AcceptanceServer, ROOT, disk_events, remote_events, write_json


def inspect(server, run_id, required):
    snapshot = server.wait(run_id, timeout=360)
    folder = Path(snapshot["result"]["run_directory"])
    events = disk_events(folder)
    responded = [e for e in events if e["type"] == "tool_responded"]
    reads = [e for e in responded if e["data"]["name"] == "read_webpage" and e["data"]["result"]["ok"]]
    searches = [e for e in responded if e["data"]["name"] == "web_search" and e["data"]["result"]["ok"]]
    continuation_calls = 0
    continuation_requests = 0
    for event in reads:
        request = next(e for e in events if e["type"] == "tool_requested" and e["data"]["call_id"] == event["data"]["call_id"])
        if "document_id" in json.loads(request["data"]["arguments"]):
            continuation_calls += 1
            continuation_requests += sum(e["type"] == "network_request" for e in events
                                         if request["sequence"] < e["sequence"] < event["sequence"])
    feedback = all(any(m["role"] == "tool" and m.get("tool_call_id") == e["data"]["call_id"]
                       for later in events if later["type"] == "model_requested" and later["sequence"] > e["sequence"]
                       for m in later["data"]["messages"]) for e in reads)
    before = (folder / "events.jsonl").read_bytes()
    server.client.get("/").raise_for_status()
    fresh = server.snapshot(run_id)
    visible = remote_events(server, run_id)
    original = {e["sequence"]: e for e in events}
    source_checks = []
    for source in snapshot["network"]["sources"]:
        if "document_id" not in source:
            continue
        doc = json.loads((folder / ("web-" + source["document_id"] + ".json")).read_text(encoding="utf-8"))
        source_checks.append(doc["content"] and doc["read_ranges"] == [[0, len(doc["content"])]]
                             and not doc["storage_truncated"])
    answer = snapshot["result"].get("answer") or ""
    checks = {"ended_with_answer": snapshot["status"] == "unverified" and bool(answer),
              "read_body": bool(reads), "body_feedback_seen_by_model": bool(reads) and feedback,
              "all_saved_bodies_read": bool(source_checks) and all(source_checks),
              "citations_exist": bool(snapshot["result"]["citation_check"]["cited"])
                and not snapshot["result"]["citation_check"]["unknown"],
              "answer_contains_body_details": all(word.lower() in answer.lower() for word in required),
              "refresh_does_not_replay": fresh == snapshot and before == (folder / "events.jsonl").read_bytes(),
              "events_match_disk": all(e == original[e["sequence"]] if not e["data"].get("display_truncated") else
                                        json.dumps(original[e["sequence"]]["data"], ensure_ascii=False).startswith(e["data"]["preview"])
                                        for e in visible),
              "no_shell": not any(e["data"]["name"] == "run_command" for e in responded)}
    return {"run_id": run_id, "run_directory": str(folder), "checks": checks,
            "search_calls": len(searches), "read_calls": len(reads), "answer": answer,
            "continuation_calls": continuation_calls, "continuation_requests": continuation_requests,
            "network": snapshot["network"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--hold-seconds", type=int, default=0, help="Bounded time for optional browser inspection")
    args = parser.parse_args()
    values = dict(dotenv_values(args.env_file), **os.environ)
    if not all(values.get(key) for key in ("HARNESS_API_KEY", "TAVILY_API_KEY")):
        raise SystemExit("Real model and Tavily keys required; no simulated fallback.")
    root = ROOT / "runs" / ("stage-seven-" + uuid4().hex)
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    config.update(network_enabled=True, runs_dir=str(root / "runs"), max_turns=18,
                  max_model_calls=22, max_completion_tokens=2048, total_timeout_seconds=330)
    server = AcceptanceServer(config, root, env_file=args.env_file)
    report = {"kind": "real-model-real-Tavily", "model": values.get("HARNESS_MODEL"),
              "config": config, "cases": [], "passed": False}
    try:
        server.start()
        print(json.dumps({"origin": server.origin, "control": str(root)}, ensure_ascii=False), flush=True)
        task = ("搜索 HTTPX 官方文档中的 timeouts 页面，选择该官方页面并用 read_webpage 阅读全部提取正文，"
                "如有 next_offset 必须用 document_id 连续续读到末尾。解释四种超时分别控制什么，"
                "并给出默认超时秒数。引用实际来源编号 [S#] 和网址；不要读写本地文件，不需要 Shell。")
        run_id = server.start_run(workspace, task)
        case = inspect(server, run_id, ["connect", "read", "write", "pool", "5"])
        case["checks"]["searched_before_reading"] = case["search_calls"] > 0
        report["cases"].append(case)
        print(json.dumps({"case": "search-and-read", "run_id": run_id, "checks": case["checks"]}), flush=True)
        task = ("直接使用 read_webpage 阅读 https://www.python-httpx.org/advanced/resource-limits/ 的全部提取正文；"
                "若有 next_offset 必须用 document_id 连续续读到末尾，不要先搜索。"
                "列出 Limits 的三个配置项及其默认值，说明限制对象，引用实际来源编号 [S#] 和网址。"
                "保留英文配置项名称；不要操作本地文件或 Shell。")
        run_id = server.start_run(workspace, task)
        case = inspect(server, run_id, ["max_keepalive_connections", "20", "max_connections", "100", "keepalive_expiry", "5"])
        case["checks"]["direct_url_without_search"] = case["search_calls"] == 0
        report["cases"].append(case)
        print(json.dumps({"case": "direct-url", "run_id": run_id, "checks": case["checks"]}), flush=True)
        task = ("直接使用 read_webpage 阅读 https://www.python-httpx.org/advanced/clients/ 的全部提取正文，"
                "仅使用此原始网址，不要替换为翻译代理网址。"
                "先读取 offset=0，再严格按照 next_offset 用 document_id 连续续读直到 null；不要跳过中间片段。"
                "读完后说明 Client 的连接复用优势，以及客户端与单次请求配置合并时 headers、params、cookies 的行为。"
                "保留英文参数名称，引用实际来源编号 [S#] 和网址；说明正文读取范围和完整性限制。不要操作本地文件或 Shell。")
        run_id = server.start_run(workspace, task)
        case = inspect(server, run_id, ["Client", "headers", "params", "cookies"])
        case["checks"]["multiple_chunks_without_refetch"] = case["continuation_calls"] >= 1 and case["continuation_requests"] == 0
        report["cases"].append(case)
        print(json.dumps({"case": "long-page", "run_id": run_id, "checks": case["checks"]}), flush=True)
        report["passed"] = all(all(case["checks"].values()) for case in report["cases"])
        if args.hold_seconds:
            print(json.dumps({"browser_origin": server.origin, "run_id": run_id,
                              "hold_seconds": min(args.hold_seconds, 300)}), flush=True)
            end = time.monotonic() + min(args.hold_seconds, 300)
            while time.monotonic() < end and not (root / "finish-browser").exists():
                time.sleep(0.5)
    finally:
        server.stop()
        report["cleanup"] = server.cleanup
        secrets = [values.get(key, "") for key in ("HARNESS_API_KEY", "TAVILY_API_KEY") if values.get(key)]
        paths = list(root.rglob("*.json")) + list(root.rglob("*.jsonl"))
        report["no_keys_in_records"] = all(not any(secret in path.read_text(encoding="utf-8") for secret in secrets) for path in paths)
        report["passed"] = report["passed"] and report["no_keys_in_records"] and bool(server.cleanup and server.cleanup.get("port_released"))
        write_json(ROOT / "docs/stage-seven-live.json", report)
        print(json.dumps({"passed": report["passed"], "cleanup": server.cleanup}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
