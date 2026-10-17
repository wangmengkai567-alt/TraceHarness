"""仅监听本机回环地址的 FastAPI 服务，同时提供页面、任务 API 和命令审批。

请求入口为 create_app，业务分发交给 RunManager，实际任务由 Runner 执行。
运行与审批状态由同一个 asyncio 事件循环管理；历史任务只读，不自动恢复。
"""
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
# 请求体限制按字节计算；事件分页和详情预览限制用于避免一次返回过多内容。
MAX_BODY = 131072
PAGE_SIZE = 50
DETAIL_LIMIT = 24000


class APIError(Exception):
    # 业务异常只携带 HTTP 状态码和安全错误码，由统一响应层转换为 JSON。
    def __init__(self, status: int, code: str):
        # 不把原始系统异常或模型服务响应直接带到网页中。
        self.status, self.code = status, code


@dataclass
class ActiveRun:
    # 活跃任务的内存状态；store 保存磁盘记录，job 是执行协程，cancel 是取消信号。
    store: RunStore
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    job: asyncio.Task | None = None
    status: str = "running"
    approval: dict | None = None
    # approval 是前端可见的审批内容，decision 是后台等待用户决定的 Future。
    decision: asyncio.Future | None = None
    approval_deadline: float = 0


def read_json(path: Path, fallback=None):
    # 尚未生成的快照返回默认值；JSON 损坏等其他错误保留给上层处理。
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback


class RunManager:
    """同一时刻只运行一个任务，所有状态修改都发生在服务端事件循环内。"""

    def __init__(self, config: Config, model_factory=None, secret_values: tuple[str, ...] = (),
                 *, approval_timeout: float | None = None):
        # model_factory 便于测试注入模型；生产默认使用真实模型适配器。
        config.validate()
        self.config, self.root = config, Path(config.runs_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.secrets = tuple(value for value in secret_values if value)
        self.model_factory = model_factory or (lambda: OpenAICompatibleModel(
            config, os.environ.get("HARNESS_API_KEY", "")))
        self.approval_timeout = approval_timeout if approval_timeout is not None else config.approval_timeout_seconds
        self.process_control_blocked = False
        # 一旦无法确认命令进程树退出，就阻止新任务；此内存标记只在服务重启后重置。
        self.active: ActiveRun | None = None
        # 终态文件是结果依据；没有终态的旧运行只展示为中断，不从磁盘恢复后台任务。
        self.jobs: set[asyncio.Task] = set()

    def folder(self, run_id: str) -> Path:
        # 从受校验的运行 ID 定位记录目录，拒绝符号链接及越出记录根目录的路径。
        if not RUN_ID.fullmatch(run_id):
            raise APIError(400, "invalid_run_id")
        folder = self.root / run_id
        if not folder.is_dir() or folder.is_symlink() or not (folder / "metadata.json").is_file():
            raise APIError(404, "run_not_found")
        if not folder.resolve().is_relative_to(self.root):
            raise APIError(404, "run_not_found")
        return folder

    def current(self, run_id: str) -> ActiveRun:
        # 取消与审批只允许操作当前仍在执行的任务，历史任务不能接收新决定。
        record = self.active
        if not record or record.store.run_id != run_id or not record.job or record.job.done():
            raise APIError(409, "run_not_active")
        return record

    def track(self, record: ActiveRun, operation):
        # 保留后台任务引用，结束后移除；关闭服务时据此等待全部任务完成清理。
        record.job = asyncio.create_task(operation)
        self.jobs.add(record.job)
        record.job.add_done_callback(self.jobs.discard)

    async def start(self, body: dict) -> dict:
        # 校验任务、工作目录和单任务限制后，才创建运行记录并异步调度。
        if self.process_control_blocked:
            raise APIError(409, "process_termination_unconfirmed_restart_required")
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
        # 先构建真实模型再创建记录；配置缺失直接报错，不静默切换到模拟模型。
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
        # 把 Web 的审批等待函数、阶段状态回调和取消事件注入 Runner。
        try:
            runner = Runner(self.config, model, secrets=self.secrets,
                            approve=lambda spec: self.request_approval(record, spec),
                            phase=lambda value: setattr(record, "status", value))
            result = await runner.run(
                task, workspace, record.cancel, store=record.store)
            self.process_control_blocked = runner.termination_unconfirmed
            record.status = result["status"]
        except asyncio.CancelledError:
            record.status = "cancelled"
            raise
        except Exception:
            # 未处理的 IO/服务商异常只返回固定错误码，避免将原始异常写入界面或日志。
            record.status = "failed"
            record.store.emit("web_run_error", code="run_start_or_storage_error")

    async def request_approval(self, record: ActiveRun, spec: dict):
        # Shell 调用暂停在这里：发布一次性审批内容，等待 decide 设置 Future 的结果。
        previous_status = record.status
        duration = self.approval_timeout
        record.approval = {**spec, "id": uuid4().hex, "run_id": record.store.run_id,
                           "type": "shell", "status": "pending", "approval_timeout_seconds": duration}
        record.approval_deadline = time.monotonic() + duration
        record.decision = asyncio.get_running_loop().create_future()
        record.status = "awaiting_approval"
        record.store.emit("approval_requested", **record.approval)
        try:
            # shield 避免等待超时直接取消共享 Future；过期/取消状态由本函数统一记录。
            decision = await asyncio.wait_for(asyncio.shield(record.decision), duration)
            if record.cancel.is_set():
                return "cancelled"
            return decision
        except TimeoutError:
            record.approval["status"] = "expired"
            record.store.emit("approval_expired", approval_id=record.approval["id"])
            return "expired"
        except asyncio.CancelledError:
            record.approval["status"] = "cancelled"
            record.store.emit("approval_cancelled", approval_id=record.approval["id"])
            raise
        finally:
            # 回到审批前的 running/verifying 阶段；已失效 Future 不允许后续请求再决定。
            if not record.decision.done():
                record.decision.cancel()
            record.status = previous_status

    async def run_demo(self, record: ActiveRun, task: str, workspace: Path):
        """演练批准、拒绝、过期和取消状态，不调用模型，也不执行真实命令。"""
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
        # 同时等待用户决定与取消；演练仍保存自己的元数据、事件和最终结果。
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
        # 设置协作式取消信号，由 Runner 处理当前操作及子进程清理；不是直接丢弃任务。
        record = self.current(run_id)
        if not record.cancel.is_set():
            record.cancel.set()
            record.store.emit("cancel_requested")
        return {"cancel_requested": True}

    async def decide(self, run_id: str, approval_id: str, body: dict):
        # 只接受 approve/reject，不能借审批接口修改已展示的命令或执行参数。
        record = self.current(run_id)
        if (set(body) != {"decision"} or not isinstance(body["decision"], str)
                or body["decision"] not in {"approve", "reject"}):
            raise APIError(400, "invalid_decision")
        if (record.cancel.is_set() or not record.approval or record.approval["id"] != approval_id
                or record.approval["status"] != "pending" or record.decision.done()
                or time.monotonic() >= record.approval_deadline):
            raise APIError(409, "approval_not_pending")
        # 同一事件循环内先更新状态再唤醒等待者，重复点击会因非 pending 状态被拒绝。
        record.approval["status"] = "approved" if body["decision"] == "approve" else "rejected"
        record.store.emit("approval_decided", approval_id=approval_id, decision=body["decision"])
        record.decision.set_result(body["decision"])
        return {"accepted": True}

    def events(self, folder: Path, after: int) -> dict:
        # 按事件序号增量分页，返回 cursor/has_more 供前端继续拉取，不重放任何操作。
        events = []
        has_more = False
        try:
            with (folder / "events.jsonl").open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        # 异常退出可能留下末尾半行 JSON，跳过坏行，不伪造缺失事件。
                        continue
                    if event["sequence"] <= after:
                        continue
                    if len(events) >= PAGE_SIZE:
                        has_more = True
                        break
                    wire = json.dumps(event["data"], ensure_ascii=False)
                    if len(wire) > DETAIL_LIMIT:
                        # 大段消息仅展示预览，但保留轮次、尝试次数等字段供界面显示进度。
                        progress = {key: event["data"][key]
                                    for key in ("turn", "attempt", "model_call", "final_response_only")
                                    if key in event["data"]}
                        event["data"] = {**progress, "preview": wire[:DETAIL_LIMIT], "display_truncated": True}
                    events.append(event)
        except FileNotFoundError:
            pass
        return {"events": events, "cursor": events[-1]["sequence"] if events else after,
                "has_more": has_more}

    def snapshot(self, run_id: str) -> dict:
        # 将磁盘快照与当前活跃状态组合成页面数据；有终态时优先采用终态文件。
        folder = self.folder(run_id)
        result = read_json(folder / "result.json")
        active = self.active if self.active and self.active.store.run_id == run_id else None
        live = bool(active and active.job and not active.job.done())
        metadata = read_json(folder / "metadata.json", {})
        state = read_json(folder / "state.json", {}) or {}
        # 产物从成功写入事件提取，独立于模型在回答或记忆中的声明。
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
                "network": read_json(folder / "sources.json", {}),
                "checks": {"verified": bool(result and result.get("verified")),
                           "evidence": result.get("checks", []) if result else [],
                           "note": ("仅所配置检查项通过，不代表全部需求已验证。" if result and result.get("verified") else
                                    "配置检查未全部通过；请查看检查证据。" if result and result.get("checks") else
                                    "尚未配置或尚未执行结果检查；模型自检可查看工具轨迹。")},
                "approval": active.approval if live else None}

    async def dispatch(self, method: str, path: str, body=None):
        # 业务路由：运行列表/详情、启动、事件增量、取消、审批；不负责 HTTP 安全校验。
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
        # 关闭服务时先发取消信号，再等待后台任务完成，使进程树清理和结果落盘有机会结束。
        if self.active and self.active.job and not self.active.job.done():
            self.active.cancel.set()
        if self.jobs:
            await asyncio.gather(*list(self.jobs), return_exceptions=True)


# 页面仅允许同源脚本、样式和连接，并禁止缓存、内容类型猜测及被外站嵌入。
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
}


def create_app(manager: RunManager, port: int = 8765) -> FastAPI:
    # Host/Origin 必须精确匹配 127.0.0.1:port；每次创建服务生成独立会话令牌。
    authority = f"127.0.0.1:{port}"
    origin = "http://" + authority
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        # FastAPI 关闭阶段等待管理器完成任务清理。
        yield
        await manager.shutdown()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager

    @app.middleware("http")
    async def protect(request: Request, call_next):
        # 先校验 Host、来源和会话令牌，防止其他网页借本地服务启动任务或批准命令。
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
                # 修改状态的请求额外要求显式同源 Origin 和 JSON 内容类型。
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
        # 同源页面先获取令牌和默认工作目录，后续 API 调用通过请求头携带令牌。
        return {"token": token, "default_workspace": str(Path.cwd()),
                "network_enabled": manager.config.network_enabled,
                "network_available": manager.config.network_enabled and bool(os.environ.get("TAVILY_API_KEY"))}

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def route(request: Request, path: str):
        # 静态资源仅映射三个固定路径，其他请求在读取有界 JSON 后交给业务路由。
        url = "/" + path
        if request.method == "GET" and url in {"/", "/app.js", "/style.css"}:
            name, mime = {"/": ("index.html", "text/html"),
                          "/app.js": ("app.js", "text/javascript"),
                          "/style.css": ("style.css", "text/css")}[url]
            return Response((STATIC / name).read_bytes(), media_type=mime)
        body = None
        if request.method == "POST":
            chunks = bytearray()
            # 按流读取并同时限制大小与等待时间，不依赖客户端自报 Content-Length。
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
        # 将业务层抛出的安全错误转换为一致的 HTTP JSON 响应。
        return JSONResponse({"error": error.code}, status_code=error.status)

    return app


async def serve(config: Config, port: int = 8765):
    # 单体服务同时托管页面和 API，只绑定 IPv4 回环地址，禁用代理头信任和访问日志。
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
