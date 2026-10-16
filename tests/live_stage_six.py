"""Complete real-model acceptance through an owned HTTP server; optional manual browser flow."""
import argparse
import json
from pathlib import Path
import time
from uuid import uuid4

from harness.files import digest
from tests.stage_six_support import AcceptanceServer, ACTIVE, ROOT, copy_sample, disk_events, remote_events, write_json


def memory_decision_evidence(events):
    # Verify a later model decision actually received persisted facts, including batched initial writes.
    targets = [e for e in events if e["type"] == "tool_requested" and e["data"]["name"] == "run_command"
               and e["data"].get("purpose") == "task"]
    for target in targets:
        requests = [e for e in events if e["type"] == "model_requested" and e["sequence"] < target["sequence"]]
        if not requests:
            continue
        request = requests[-1]
        memories = [e for e in events if e["type"] == "memory_updated" and e["sequence"] < request["sequence"]]
        if not memories:
            continue
        memory = memories[-1]
        facts = [finding for finding in memory["data"]["memory"]["findings"]
                 if finding["kind"] == "fact" and finding["source"] == "input.txt"]
        if not all(value in json.dumps(facts, ensure_ascii=False) for value in ("47", "58", "105", "315")):
            continue
        if any(m.get("role") == "system" and "Current plan and task memory:" in m.get("content", "")
               and all(value in m["content"] for value in ("input.txt", "105", "315"))
               for m in request["data"]["messages"]):
            return {"memory_event": memory["sequence"], "model_request": request["sequence"],
                    "later_tool_request": target["sequence"], "later_tool": target["data"]["name"]}
    return None


def inspect_run(server, workspace, run_id):
    snapshot = server.wait(run_id)
    folder = Path(snapshot["result"]["run_directory"])
    events = disk_events(folder)
    visible = remote_events(server, run_id)
    by_sequence = {event["sequence"]: event for event in events}
    correspondence = all(event == by_sequence[event["sequence"]] if not event["data"].get("display_truncated") else
                         json.dumps(by_sequence[event["sequence"]]["data"], ensure_ascii=False).startswith(event["data"]["preview"])
                         for event in visible)
    requested = [e for e in events if e["type"] == "approval_requested"]
    approved = {e["data"]["approval_id"] for e in events if e["type"] == "approval_decided" and e["data"]["decision"] == "approve"}
    starts = [e for e in events if e["type"] == "shell_process_started"]
    memories = [e for e in events if e["type"] == "memory_updated"]
    memory_evidence = memory_decision_evidence(events)
    expected = "资料摘要\n合计：105\n乘三结果：315\n来源：input.txt\n".encode()
    before = folder.joinpath("events.jsonl").read_bytes()
    server.client.get("/").raise_for_status()
    same = server.snapshot(run_id) == snapshot
    no_replay = folder.joinpath("events.jsonl").read_bytes() == before
    source = ROOT / "examples/stage_six"
    checks = {
        "completed_with_configured_checks": snapshot["status"] == "completed" and snapshot["result"]["verified"]
            and len(snapshot["result"]["checks"]) == 2 and all(item["passed"] for item in snapshot["result"]["checks"]),
        "rules_snapshot_matches": snapshot["rules"]["sha256"] == digest(workspace.joinpath("AGENTS.md").read_bytes()),
        "plan_and_memory_recorded": bool(snapshot["state"].get("plan")) and bool(memories),
        "earlier_memory_used_in_later_decision": memory_evidence is not None,
        "bounded_history_exercised": any(e["type"] == "context_history_limited" for e in events),
        "exact_artifact_bytes": workspace.joinpath("summary.txt").is_file() and workspace.joinpath("summary.txt").read_bytes() == expected,
        "inputs_rules_checker_unchanged": all(workspace.joinpath(name).read_bytes() == source.joinpath(name).read_bytes()
                                               for name in ("input.txt", "AGENTS.md", "verify.py", "task.txt")),
        "two_independent_approvals": len(requested) == len(approved) == len(starts) == 2
            and len({e["data"]["id"] for e in requested}) == 2,
        "every_process_has_prior_exact_approval": all(any(
            req["data"]["call_id"] == start["data"]["call_id"] and req["data"]["id"] in approved
            and req["data"]["command"] == "python verify.py" and req["data"]["workspace"] == str(workspace)
            and req["data"]["timeout_seconds"] == 15 and req["sequence"] < start["sequence"]
            and any(e["type"] == "approval_decided" and e["data"]["approval_id"] == req["data"]["id"]
                    and req["sequence"] < e["sequence"] < start["sequence"] for e in events)
            for req in requested) for start in starts),
        "both_shell_trees_stopped": sum(e["type"] == "shell_process_stopped" for e in events) == 2,
        "final_answer_uses_result": "315" in (snapshot["result"]["answer"] or ""),
        "event_sequence_and_page_correspondence": correspondence and [e["sequence"] for e in visible] == list(range(1, len(events) + 1)),
        "refresh_reads_only": same and no_replay,
        "artifacts_and_checks_match_disk": snapshot["result"]["artifacts"] == snapshot["artifacts"]
            and snapshot["result"] == json.loads(folder.joinpath("result.json").read_text(encoding="utf-8")),
    }
    return {"run_id": run_id, "model": snapshot["metadata"]["config"]["model"],
            "config": snapshot["metadata"]["config"], "started_at": snapshot["metadata"]["started_at"],
            "finished_at": snapshot["result"]["finished_at"], "workspace": str(workspace),
            "run_directory": str(folder), "event_count": len(events), "approvals": [e["data"] for e in requested],
            "memory_evidence": memory_evidence,
            "checks": checks, "passed": all(checks.values()), "result": snapshot["result"]}


def main(args):
    root = ROOT / "runs" / ("stage-six-" + uuid4().hex)
    workspace = copy_sample(root / "workspace")
    config = json.loads(workspace.joinpath("config.json").read_text(encoding="utf-8"))
    config["runs_dir"] = str(root / "records")
    server = AcceptanceServer(config, root, env_file=ROOT / ".env")
    manifest = {"root": str(root), "workspace": str(workspace), "origin": server.origin,
                "task": workspace.joinpath("task.txt").read_text(encoding="utf-8"), "control": str(server.control)}
    write_json(root / "manifest.json", manifest)
    write_json(ROOT / "runs/stage-six-current.json", manifest)
    report = None
    try:
        with server:
            print(json.dumps({"manifest": str(root / "manifest.json"), "origin": server.origin,
                              "workspace": str(workspace), "browser": args.browser}, ensure_ascii=False), flush=True)
            if args.browser:
                end = time.monotonic() + 650
                while not root.joinpath("finish").exists():
                    if time.monotonic() >= end:
                        raise TimeoutError("browser acceptance time exceeded")
                    time.sleep(0.1)
                runs = server.client.get("/api/runs").raise_for_status().json()["runs"]
                if len(runs) != 1:
                    raise AssertionError("browser acceptance must create exactly one run")
                run_id = runs[0]["run_id"]
            else:
                run_id = server.start_run(workspace, manifest["task"])
                approvals = set()
                end = time.monotonic() + 520
                while True:
                    snapshot = server.snapshot(run_id)
                    if snapshot["status"] not in ACTIVE:
                        break
                    approval = snapshot["approval"]
                    if approval and approval["status"] == "pending":
                        if (approval["command"] != "python verify.py" or approval["timeout_seconds"] != 15
                                or approval["workspace"] != str(workspace) or len(approvals) >= 2):
                            raise AssertionError("unexpected command; not approved")
                        approvals.add(approval["id"])
                        server.client.post(f"/api/runs/{run_id}/approvals/{approval['id']}", json={"decision": "approve"}).raise_for_status()
                    if time.monotonic() >= end:
                        raise TimeoutError("real model acceptance time exceeded")
                    time.sleep(0.1)
            report = inspect_run(server, workspace, run_id)
            report["entry"] = "real_browser" if args.browser else "real_http_api"
            if root.joinpath("browser.json").exists():
                report["browser_observations"] = json.loads(root.joinpath("browser.json").read_text(encoding="utf-8"))
    finally:
        if server.cleanup is None:
            server.stop()
        if report is not None:
            report["server_cleanup"] = server.cleanup
            report["passed"] &= bool(server.cleanup and server.cleanup["process_exited"] and server.cleanup["port_released"])
            write_json(ROOT / "docs/stage-six-live.json", report)
            print(json.dumps({"run_id": report["run_id"], "checks": report["checks"],
                              "server_cleanup": server.cleanup, "passed": report["passed"]}, ensure_ascii=False), flush=True)
    return 0 if report and report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", action="store_true", help="Wait for UI task submission/approvals and a finish control file")
    raise SystemExit(main(parser.parse_args()))
