"""Loopback-only FastAPI entry. One asyncio loop owns all runs and approvals."""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .config import Config
from .models import OpenAICompatibleModel
from .runner import Runner
from .store import RUN_ID, RUN_ID_PATTERN, RunStore, utc_now

STATIC = Path(__file__).with_name("static")
MAX_BODY = 131072
PAGE_SIZE = 50
DETAIL_LIMIT = 24000


class APIError(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code


@dataclass
class ActiveRun:
    store: RunStore
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    job: asyncio.Task | None = None
    status: str = "running"
    approval: dict | None = None
    decision: asyncio.Future | None = None
    approval_deadline: float = 0


def read_json(path: Path, fallback=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback


class RunManager:
    """One active task; all mutations happen on the serving asyncio loop."""

    def __init__(self, config: Config, model_factory=None, secret_values: tuple[str, ...] = (),
                 *, approval_timeout: float = 60):
        config.validate()
        self.config, self.root = config, Path(config.runs_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.secrets = tuple(value for value in secret_values if value)
        self.model_factory = model_factory or (lambda: OpenAICompatibleModel(
            config, os.environ.get("HARNESS_API_KEY", "")))
        self.approval_timeout = approval_timeout
        self.active: ActiveRun | None = None
        # A terminal result is authoritative; old unfinished runs are displayed, never resumed.
        self.jobs: set[asyncio.Task] = set()

    def folder(self, run_id: str) -> Path:
        if not RUN_ID.fullmatch(run_id):
            raise APIError(400, "invalid_run_id")
        folder = self.root / run_id
        if not folder.is_dir() or folder.is_symlink() or not (folder / "metadata.json").is_file():
            raise APIError(404, "run_not_found")
        if not folder.resolve().is_relative_to(self.root):
            raise APIError(404, "run_not_found")
        return folder

    def current(self, run_id: str) -> ActiveRun:
        record = self.active
        if not record or record.store.run_id != run_id or not record.job or record.job.done():
            raise APIError(409, "run_not_active")
        return record

    def track(self, record: ActiveRun, operation):
        record.job = asyncio.create_task(operation)
        self.jobs.add(record.job)
        record.job.add_done_callback(self.jobs.discard)

    async def start(self, body: dict) -> dict:
        if set(body) - {"task", "workspace", "mode"}:
            raise APIError(400, "unknown_fields")
        task, workspace, mode = body.get("task"), body.get("workspace"), body.get("mode", "model")
        if not isinstance(task, str) or not task.strip() or len(task) > self.config.max_input_chars:
            raise APIError(400, "invalid_task")
        if (not isinstance(workspace, str) or not workspace.strip() or not isinstance(mode, str)
                or mode not in {"model", "approval_demo"}):
            raise APIError(400, "invalid_workspace_or_mode")
        try:
            path = Path(workspace).resolve(strict=True)
            if not path.is_dir():
                raise ValueError
        except (OSError, ValueError, RuntimeError):
            raise APIError(400, "workspace_must_exist") from None
        if self.active and self.active.job and not self.active.job.done():
            raise APIError(409, "another_run_is_active")
        # Construct the real model before creating a run; missing config is never a fallback.
        try:
            model = self.model_factory() if mode == "model" else None
        except ValueError:
            raise APIError(503, "model_configuration_unavailable") from None
        store = RunStore(self.root, self.secrets)
        record = ActiveRun(store)
        self.active = record
        if mode == "model":
            self.track(record, self.run_model(record, task, path, model))
        else:
            self.track(record, self.run_demo(record, task, path))
        return {"run_id": store.run_id}

    async def run_model(self, record: ActiveRun, task: str, workspace: Path, model):
        try:
            result = await Runner(self.config, model, secrets=self.secrets).run(
                task, workspace, record.cancel, store=record.store)
            record.status = result["status"]
        except asyncio.CancelledError:
            record.status = "cancelled"
            raise
        except Exception:
            # Keep raw IO/provider exceptions out of the UI and logs.
            record.status = "failed"
            record.store.emit("web_run_error", code="run_start_or_storage_error")

    async def run_demo(self, record: ActiveRun, task: str, workspace: Path):
        """An approval state-machine exercise, never a model or command executor."""
        store = record.store
        started = time.monotonic()
        store.write_json("metadata.json", {"schema_version": 1, "run_id": store.run_id,
                         "task": task, "workspace": str(workspace), "mode": "approval_demo",
                         "started_at": utc_now(), "config": self.config.snapshot()})
        store.emit("run_started", provider="approval_demo")
        duration = min(self.approval_timeout, self.config.total_timeout_seconds)
        record.approval_deadline = time.monotonic() + duration
        record.approval = {"id": uuid4().hex, "run_id": store.run_id,
                           "type": "simulation", "label": "模拟操作：记录一次批准结果，不执行命令或文件写入",
                           "workspace": str(workspace), "timeout_seconds": duration,
                           "status": "pending"}
        record.decision = asyncio.get_running_loop().create_future()
        record.status = "awaiting_approval"
        store.emit("approval_requested", **record.approval)
        cancelled = asyncio.create_task(record.cancel.wait())
        status, reason, answer = "unverified", "approval_demo_rejected", "模拟操作已拒绝。"
        try:
            done, _ = await asyncio.wait({record.decision, cancelled}, timeout=duration,
                                         return_when=asyncio.FIRST_COMPLETED)
            if record.cancel.is_set():
                status, reason, answer = "cancelled", "user_cancelled", "模拟运行已取消。"
                record.approval["status"] = "cancelled"
                store.emit("approval_cancelled", approval_id=record.approval["id"])
            elif record.decision not in done:
                status, reason, answer = "budget_exceeded", "approval_timeout", "模拟审批已过期。"
                record.approval["status"] = "expired"
                store.emit("approval_expired", approval_id=record.approval["id"])
            elif record.decision.result() == "approve":
                reason, answer = "approval_demo_completed", "已批准：模拟操作完成，未执行真实命令。"
                store.emit("simulated_action_completed", approval_id=record.approval["id"])
        except asyncio.CancelledError:
            status, reason, answer = "cancelled", "server_shutdown", "服务关闭，模拟运行已取消。"
            record.approval["status"] = "cancelled"
            store.emit("approval_cancelled", approval_id=record.approval["id"])
        finally:
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
            if not record.decision.done():
                record.decision.cancel()
            record.status = status
            result = {"schema_version": 1, "run_id": store.run_id, "status": status,
                      "reason": reason, "answer": answer, "verified": False, "state": None,
                      "finished_at": utc_now(), "elapsed_seconds": round(time.monotonic() - started, 3),
                      "run_directory": str(store.directory)}
            store.emit("run_finished", **result)
            store.write_json("result.json", result)

    async def cancel(self, run_id: str):
        record = self.current(run_id)
        if not record.cancel.is_set():
            record.cancel.set()
            record.store.emit("cancel_requested")
        return {"cancel_requested": True}

    async def decide(self, run_id: str, approval_id: str, body: dict):
        record = self.current(run_id)
        if (set(body) != {"decision"} or not isinstance(body["decision"], str)
                or body["decision"] not in {"approve", "reject"}):
            raise APIError(400, "invalid_decision")
        if (record.cancel.is_set() or not record.approval or record.approval["id"] != approval_id
                or record.approval["status"] != "pending" or record.decision.done()
                or time.monotonic() >= record.approval_deadline):
            raise APIError(409, "approval_not_pending")
        record.approval["status"] = "approved" if body["decision"] == "approve" else "rejected"
        record.store.emit("approval_decided", approval_id=approval_id, decision=body["decision"])
        record.decision.set_result(body["decision"])
        return {"accepted": True}

    def events(self, folder: Path, after: int) -> dict:
        events = []
        has_more = False
        try:
            with (folder / "events.jsonl").open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        # A crash may leave a partial last line; never fabricate an event.
                        continue
                    if event["sequence"] <= after:
                        continue
                    if len(events) >= PAGE_SIZE:
                        has_more = True
                        break
                    wire = json.dumps(event["data"], ensure_ascii=False)
                    if len(wire) > DETAIL_LIMIT:
                        event["data"] = {"preview": wire[:DETAIL_LIMIT], "display_truncated": True}
                    events.append(event)
        except FileNotFoundError:
            pass
        return {"events": events, "cursor": events[-1]["sequence"] if events else after,
                "has_more": has_more}

    def snapshot(self, run_id: str) -> dict:
        folder = self.folder(run_id)
        result = read_json(folder / "result.json")
        active = self.active if self.active and self.active.store.run_id == run_id else None
        live = bool(active and active.job and not active.job.done())
        metadata = read_json(folder / "metadata.json", {})
        state = read_json(folder / "state.json", {}) or {}
        # Artifact evidence comes from successful write events, separately from model claims.
        artifacts = {}
        lines = (folder / "events.jsonl").read_text(encoding="utf-8").splitlines() if (folder / "events.jsonl").exists() else []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            data = event.get("data", {})
            if event["type"] == "tool_responded" and data.get("name") == "write_file":
                wire = data.get("result", {})
                evidence = wire.get("data")
                if wire.get("ok") and isinstance(evidence, dict) and "path" in evidence:
                    artifacts[evidence["path"]] = evidence
        return {"run_id": run_id, "status": result["status"] if result else (
                    "cancelling" if live and active.cancel.is_set() else active.status if live else "interrupted"),
                "metadata": metadata, "state": state, "rules": read_json(folder / "rules.json"),
                "result": result, "artifacts": list(artifacts.values()),
                "checks": {"verified": bool(result and result.get("verified")),
                           "note": "尚未配置通用结果验证器；模型自检可查看工具轨迹。"},
                "approval": active.approval if live else None}

    async def dispatch(self, method: str, path: str, body=None):
        if method == "GET" and path == "/api/runs":
            runs = []
            for folder in sorted(self.root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                if RUN_ID.fullmatch(folder.name) and folder.is_dir() and (folder / "metadata.json").is_file():
                    try:
                        snapshot = self.snapshot(folder.name)
                        runs.append({"run_id": folder.name, "status": snapshot["status"],
                                     "task": snapshot["metadata"].get("task", "")[:160]})
                    except (OSError, ValueError, APIError):
                        continue
                if len(runs) >= 100:
                    break
            return {"runs": runs}
        if method == "POST" and path == "/api/runs":
            return await self.start(body)
        match = re.fullmatch(rf"/api/runs/({RUN_ID_PATTERN})(?:/(.*))?", path)
        if not match:
            raise APIError(404, "not_found")
        run_id, action = match.groups()
        if method == "GET" and action is None:
            return self.snapshot(run_id)
        if method == "GET" and action and action.startswith("events?after="):
            cursor = action.removeprefix("events?after=")
            if not cursor.isascii() or not cursor.isdigit() or len(cursor) > 12:
                raise APIError(400, "invalid_cursor")
            return self.events(self.folder(run_id), int(cursor))
        if method == "POST" and action == "cancel":
            if body:
                raise APIError(400, "unexpected_body")
            return await self.cancel(run_id)
        if method == "POST" and action and action.startswith("approvals/"):
            return await self.decide(run_id, action.removeprefix("approvals/"), body)
        raise APIError(404, "not_found")

    async def shutdown(self):
        if self.active and self.active.job and not self.active.job.done():
            self.active.cancel.set()
        if self.jobs:
            await asyncio.gather(*list(self.jobs), return_exceptions=True)


SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
}


def create_app(manager: RunManager, port: int = 8765) -> FastAPI:
    authority = f"127.0.0.1:{port}"
    origin = "http://" + authority
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await manager.shutdown()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager

    @app.middleware("http")
    async def protect(request: Request, call_next):
        try:
            if request.headers.get("host") != authority:
                raise APIError(403, "invalid_host")
            source = request.headers.get("origin")
            if source is not None and source != origin:
                raise APIError(403, "invalid_origin")
            if request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
                raise APIError(403, "cross_site_request")
            if request.url.path.startswith("/api/") and request.url.path != "/api/session":
                if not secrets.compare_digest(request.headers.get("x-harness-token", ""), token):
                    raise APIError(403, "invalid_token")
            if request.method not in {"GET", "POST"}:
                raise APIError(405, "method_not_allowed")
            if request.method == "POST":
                if source != origin:
                    raise APIError(403, "origin_required")
                if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                    raise APIError(415, "json_required")
            response = await call_next(request)
        except APIError as error:
            response = JSONResponse({"error": error.code}, status_code=error.status)
        except Exception:
            response = JSONResponse({"error": "server_error"}, status_code=500)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.get("/api/session")
    async def session():
        return {"token": token, "default_workspace": str(Path.cwd())}

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def route(request: Request, path: str):
        url = "/" + path
        if request.method == "GET" and url in {"/", "/app.js", "/style.css"}:
            name, mime = {"/": ("index.html", "text/html"),
                          "/app.js": ("app.js", "text/javascript"),
                          "/style.css": ("style.css", "text/css")}[url]
            return Response((STATIC / name).read_bytes(), media_type=mime)
        body = None
        if request.method == "POST":
            chunks = bytearray()
            try:
                async with asyncio.timeout(5):
                    async for chunk in request.stream():
                        chunks.extend(chunk)
                        if len(chunks) > MAX_BODY:
                            raise APIError(413, "body_too_large")
            except TimeoutError:
                raise APIError(408, "body_timeout") from None
            try:
                body = json.loads(chunks)
            except (ValueError, UnicodeError, RecursionError):
                raise APIError(400, "invalid_json") from None
            if not isinstance(body, dict):
                raise APIError(400, "json_object_required")
        if request.url.query:
            url += "?" + request.url.query
        return await manager.dispatch(request.method, url, body)

    @app.exception_handler(APIError)
    async def api_error(request, error):
        return JSONResponse({"error": error.code}, status_code=error.status)

    return app


async def serve(config: Config, port: int = 8765):
    import uvicorn
    manager = RunManager(config, secret_values=tuple(os.environ.get(key, "") for key in (
        "HARNESS_API_KEY", "TAVILY_API_KEY")))
    app = create_app(manager, port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                            access_log=False, proxy_headers=False, log_level="warning"))
    print(f"Harness Web: http://127.0.0.1:{port}", flush=True)
    try:
        await server.serve()
    finally:
        await manager.shutdown()
