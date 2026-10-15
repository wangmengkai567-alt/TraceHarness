"""Web acceptance against a running local server, optionally rechecking a saved run."""
import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path
from uuid import uuid4

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--run-id", help="Inspect a completed run instead of starting a new model task")
    args = parser.parse_args()
    origin = f"http://127.0.0.1:{args.port}"
    with httpx.Client(base_url=origin, timeout=10, trust_env=False) as client:
        session = client.get("/api/session").raise_for_status().json()
        client.headers.update({"X-Harness-Token": session["token"], "Origin": origin})
        run_id = args.run_id
        if not run_id:
            workspace = Path("runs") / ("stage-three-workspace-" + uuid4().hex)
            shutil.copytree("examples/stage_two", workspace)
            task = (workspace / "task.txt").read_text(encoding="utf-8") + "\n计划最多三个步骤，记忆与回复保持简洁。"
            run_id = client.post("/api/runs", json={"task": task, "workspace": str(workspace.resolve()),
                                                   "mode": "model"}).raise_for_status().json()["run_id"]
        deadline = time.monotonic() + 420
        while True:
            snapshot = client.get(f"/api/runs/{run_id}").raise_for_status().json()
            if snapshot["status"] not in {"running", "cancelling", "awaiting_approval"}:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("acceptance wait exceeded; inspect/cancel the backend run explicitly")
            time.sleep(0.5)
        events, cursor = [], 0
        while True:
            page = client.get(f"/api/runs/{run_id}/events?after={cursor}").raise_for_status().json()
            events.extend(page["events"])
            cursor = page["cursor"]
            if not page["has_more"]:
                break
        folder = Path(snapshot["result"]["run_directory"])
        disk_events = [json.loads(line) for line in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        original = {event["sequence"]: event for event in disk_events}
        correspondence = True
        for event in events:
            expected = original[event["sequence"]]
            if event["data"].get("display_truncated"):
                correspondence &= json.dumps(expected["data"], ensure_ascii=False).startswith(event["data"]["preview"])
            else:
                correspondence &= event == expected
        workspace = Path(snapshot["metadata"]["workspace"])
        artifact = workspace / "summary.txt"
        raw = artifact.read_bytes() if artifact.is_file() else b""
        text = raw.decode("utf-8-sig")
        # Simulate refresh as GET-only traffic and check that no execution event was added.
        client.get("/").raise_for_status()
        reloaded = client.get(f"/api/runs/{run_id}").raise_for_status().json()
        no_more = client.get(f"/api/runs/{run_id}/events?after={cursor}").raise_for_status().json()
        checks = {
            "normal_unverified_terminal": snapshot["status"] == "unverified" and not snapshot["checks"]["verified"],
            "file_matches_sample_rules": text.startswith("资料摘要") and "42" in text and "来源：input.txt" in text
                                         and b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf"),
            "event_sequence_exact": [e["sequence"] for e in events] == list(range(1, len(disk_events) + 1)),
            "page_log_correspondence": correspondence,
            "snapshot_matches_disk": snapshot["result"] == json.loads((folder / "result.json").read_text(encoding="utf-8"))
                                     and snapshot["state"] == json.loads((folder / "state.json").read_text(encoding="utf-8"))
                                     and snapshot["rules"] == json.loads((folder / "rules.json").read_text(encoding="utf-8")),
            "artifact_evidence_matches_file": any(a["path"] == "summary.txt" and a["sha256"] == hashlib.sha256(raw).hexdigest()
                                                   for a in snapshot["artifacts"]),
            "refresh_no_execution": reloaded == snapshot and not no_more["events"]
                                    and len(disk_events) == len((folder / "events.jsonl").read_text(encoding="utf-8").splitlines()),
            "plan_completed": bool(snapshot["state"].get("plan")) and all(s["status"] == "completed" for s in snapshot["state"]["plan"]),
        }
        report = {"run_id": run_id, "model": snapshot["metadata"]["config"]["model"],
                  "config": snapshot["metadata"]["config"], "finished_at": snapshot["result"]["finished_at"],
                  "event_count": len(events), "checks": checks, "passed": all(checks.values()),
                  "workspace": str(workspace), "run_directory": str(folder)}
        Path("docs/stage-three-live.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                                     encoding="utf-8", newline="\n")
        print(json.dumps({"run_id": run_id, "event_count": len(events), "checks": checks,
                          "passed": report["passed"]}, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
