"""任务执行主循环：请求模型 → 执行工具 → 回填结果 → 回答或独立验收。

Runner 统一控制总预算、取消、重试和恢复，具体操作委托给工具模块。
阅读 run 时可先看内部四个辅助函数，再看下方初始化和逐轮执行的主流程。
"""
from __future__ import annotations

import asyncio
import json
import time
import copy
import os
from pathlib import Path
from typing import Awaitable, Protocol, TypeVar

from .config import Config
from .contracts import ModelError, ModelReply, Status, ToolCall, ToolResult
from .models import Model, parse_reply
from .store import RunStore, utc_now
from .arithmetic import ArithmeticTools
from .tool_results import bound_result
from .files import FileError, FileTools
from .context import RunTools, TaskContext
from .processes import TerminationUnconfirmed
from .shell import ShellTools
from .recovery import Recovery, REPAIRABLE_MODEL_ERRORS
from .verification import verify
from .search import NetworkTools, check_public_dns

T = TypeVar("T")


class Tools(Protocol):
    # 工具集接口：schemas 提供模型可见声明，execute 根据一次调用返回结构化结果。
    def schemas(self) -> list[dict]: ...
    async def execute(self, call: ToolCall) -> ToolResult: ...


class Halt(Exception):
    # 内部控制信号，携带可预期的终止状态；最外层捕获后统一写入最终结果。
    def __init__(self, status: Status, reason: str):
        # reason 是机器可读原因码，供日志和界面解释预算耗尽、取消等情况。
        self.status, self.reason = status, reason


class Runner:
    # 一个实例接收模型和能力依赖；每次 run 的计数、上下文及恢复状态在函数内创建。
    def __init__(self, config: Config, model: Model, *, tools: Tools | None = None,
                 secrets: tuple[str, ...] = (), approve=None, phase=None,
                 network_transport=None, network_dns_check=check_public_dns):
        # approve 由 Web 注入；未提供时不开放 Shell。phase 用来通知 Web 当前是否在验收。
        config.validate()
        self.config, self.model = config, model
        self.tools = tools if tools is not None else ArithmeticTools()
        self.secrets = secrets
        self.approve = approve
        self.phase = phase if phase is not None else lambda value: None
        self.termination_unconfirmed = False
        self.network_transport, self.network_dns_check = network_transport, network_dns_check

    async def run(self, task: str, workspace: Path,
                  cancel: asyncio.Event | None = None, *, store: RunStore | None = None) -> dict:
        # 先检查任务与目录，随后建立记录、能力包装和本次运行独立的预算/状态。
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must not be empty")
        workspace = workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("workspace must be a directory")
        cancel = cancel if cancel is not None else asyncio.Event()
        config = self.config
        network_key = os.environ.get("TAVILY_API_KEY", "")
        store = store if store is not None else RunStore(Path(config.runs_dir).resolve(), self.secrets)
        if network_key and network_key not in store.secrets:
            store.secrets += (network_key,)
        network = NetworkTools(self.tools, config, store, network_key,
                               transport=self.network_transport, dns_check=self.network_dns_check)
        network.save()
        started = time.monotonic()
        # 使用单调时钟计算时限，避免系统时间调整影响总预算；日志时间另用 UTC。
        deadline = started + config.total_timeout_seconds
        turns = 0
        model_calls = 0
        tool_calls = 0
        usage: dict[str, int] = {}
        used_ids: set[str] = set()
        # history 按“模型回复 + 对应工具反馈”分组，供 context 按整组裁剪。
        history: list[list[dict]] = []
        context = None
        run_tools = self.tools
        recovery = None
        operations = []
        artifacts = {}
        check_evidence = []
        verification_attempts = 0
        model_repairs = 0
        concise_recovery = False
        checks = copy.deepcopy(config.checks)
        # 固定本次验收配置，避免外部修改列表影响正在运行的任务。
        store.write_json("metadata.json", {
            "schema_version": 1, "run_id": store.run_id, "started_at": utc_now(),
            "workspace": str(workspace), "task": task[:config.max_input_chars],
            "task_truncated": len(task) > config.max_input_chars,
            "config": config.snapshot(), "mode": config.provider,
            "final_response_policy": "reserve_last_turn",
            "owner_pid": os.getpid(),
        })
        store.emit("run_started", provider=config.provider)

        def check() -> None:
            # 在关键步骤前后共同检查取消和总时限，通过 Halt 跳到统一收尾路径。
            if cancel.is_set():
                raise Halt(Status.CANCELLED, "user_cancelled")
            if time.monotonic() >= deadline:
                raise Halt(Status.BUDGET_EXCEEDED, "total_timeout")

        async def controlled(operation: Awaitable[T], timeout: float, code: str) -> T:
            # 同时等待操作完成和用户取消，并取“局部时限/剩余总时限”中较短的一个。
            remaining = deadline - time.monotonic()
            job = asyncio.ensure_future(operation)
            cancelled = asyncio.create_task(cancel.wait())
            try:
                check()
                done, _ = await asyncio.wait({job, cancelled}, timeout=min(timeout, remaining),
                                             return_when=asyncio.FIRST_COMPLETED)
                check()
                if cancelled in done:
                    raise Halt(Status.CANCELLED, "user_cancelled")
                if job not in done:
                    raise ModelError(code, retryable=code == "model_request_timeout")
                return job.result()
            finally:
                # 等待被取消的操作完成清理；若进程树无法确认退出，必须升级为运行失败。
                for pending in (job, cancelled):
                    if not pending.done():
                        pending.cancel()
                outcomes = await asyncio.gather(job, cancelled, return_exceptions=True)
                if any(isinstance(outcome, TerminationUnconfirmed) for outcome in outcomes):
                    self.termination_unconfirmed = True
                    raise Halt(Status.FAILED, "process_termination_unconfirmed")

        def finish(status: Status, reason: str, answer: str | None = None) -> dict:
            # 所有终止路径共用：汇总回答、预算、证据、未决副作用和未完成计划并落盘。
            for item in check_evidence:
                if item.get("status") == "running":
                    item.update(status="interrupted", passed=False, error="check_interrupted")
            result = store.redact({
                "schema_version": 1, "run_id": store.run_id, "status": status.value,
                "reason": reason, "answer": answer, "verified": status == Status.COMPLETED,
                "turns": turns, "model_calls": model_calls, "tool_calls": tool_calls,
                "usage": usage, "elapsed_seconds": round(time.monotonic() - started, 3),
                "finished_at": utc_now(), "run_directory": str(store.directory),
                "state": context.snapshot() if context else None,
                "operations": operations, "artifacts": list(artifacts.values()),
                "checks": check_evidence, "verification_attempts": verification_attempts,
                "unresolved": ((context.memory["unresolved"] if context else [])
                               + list(recovery.unresolved.values()) if recovery else []),
                "unfinished_steps": [step for step in context.plan if step["status"] != "completed"] if context else [],
                "network": network.summary(), "citation_check": network.citation_check(answer),
                "completion_scope": "Only configured checks are verified." if status == Status.COMPLETED else
                                    "No verified completion; consult checks and unfinished work.",
            })
            store.emit("run_finished", **result)
            store.write_json("result.json", result)
            return result

        async def dispatch(call: ToolCall, purpose="task") -> ToolResult:
            # 工具和验收共用的执行入口：计数 → 记录 → 恢复检查 → 执行 → 保存反馈。
            nonlocal tool_calls
            check()
            if tool_calls >= config.max_tool_calls:
                raise Halt(Status.BUDGET_EXCEEDED, "tool_call_limit")
            tool_calls += 1
            store.emit("tool_requested", call_id=call.id, name=call.name, arguments=call.arguments, purpose=purpose)
            record = {"call_id": call.id, "name": call.name, "purpose": purpose,
                      "ok": None, "error": "operation_interrupted_or_outcome_unknown"}
            operations.append(record)
            # 先登记“结果未知”，即使执行中断，报告仍能反映这次操作曾被请求。
            result = recovery.blocked(call)
            # 配置检查允许重复执行，但仍需新审批；已被用户拒绝的命令不会因此放行。
            if purpose == "verification" and result and result.error == "side_effect_uncertain_no_replay":
                result = None
            if result is None:
                try:
                    # Shell 外层时限要容纳审批等待与命令执行；各阶段仍受总时限约束。
                    timeout = (config.approval_timeout_seconds + config.shell_timeout_seconds + 1
                               if call.name == "run_command" and self.approve else
                               config.search_timeout_seconds if call.name == "web_search" else
                               config.extract_timeout_seconds if call.name == "read_webpage" else config.tool_timeout_seconds)
                    result = await controlled(run_tools.execute(call), timeout,
                                              "network_timeout" if call.name in {"web_search", "read_webpage"} else "tool_timeout")
                except TerminationUnconfirmed:
                    self.termination_unconfirmed = True
                    raise Halt(Status.FAILED, "process_termination_unconfirmed") from None
                except Halt:
                    raise
                except ModelError as error:
                    result = ToolResult(False, error=error.code)
                except Exception:
                    result = ToolResult(False, error="tool_execution_error")
            recovery.observe(call, result)
            # 操作证据由真实返回值提取；模型声称“已创建”的文件不直接列为产物。
            record.update(ok=result.ok, error=result.error)
            if isinstance(result.data, dict):
                record["evidence"] = {key: value for key, value in result.data.items()
                                      if key in {"path", "sha256", "bytes", "exit_code", "timed_out"}}
                if result.ok and call.name == "write_file" and "path" in result.data:
                    artifacts[result.data["path"]] = result.data
            result = bound_result(result, config.max_output_chars)
            store.emit("tool_responded", call_id=call.id, name=call.name, result=result.wire(), purpose=purpose)
            check()
            if recovery.repeats >= config.max_consecutive_failures:
                raise Halt(Status.FAILED, "repeated_tool_failure")
            return result

        try:
            # 第一阶段：加载规范快照、初始化计划记忆，并组合本次可用的工具。
            check()
            files = FileTools(workspace, config.max_file_bytes, config.max_directory_entries)
            try:
                rules = files.rules(config.max_rules_bytes)
            except FileError as error:
                raise Halt(Status.FAILED, "rules_" + str(error)) from None
            except (OSError, ValueError, RuntimeError):
                raise Halt(Status.FAILED, "rules_access_error") from None
            store.write_json("rules.json", rules)
            store.emit("rules_loaded", **rules)
            context = TaskContext(task, rules, store, config.max_state_chars)
            store.write_json("state.json", context.snapshot())
            core = (ShellTools(workspace, config, self.approve, store.emit, cancel, network)
                    if self.approve else network)
            run_tools = RunTools(core, files, context)
            recovery = Recovery(files)
            # 工具组合顺序：RunTools → 状态/文件，或 ShellTools → NetworkTools → 基础工具。
            for turn in range(1, config.max_turns + 1):
                # 第二阶段：组装当前上下文；历史可裁剪，任务、规范、计划和记忆始终保留。
                check()
                turns = turn
                messages, dropped = context.assemble(history, config.recent_history_turns)
                messages.insert(1, {"role": "system", "content": network.prompt()})
                if checks:
                    messages.insert(1, {"role": "system", "content": "Operator-configured verification checks: "
                        + json.dumps(checks, ensure_ascii=False) + ". On your final answer the executor runs these checks. "
                        "If they fail, repair within remaining budgets and answer again. Each shell check needs approval. "
                        "Previous check evidence: " + json.dumps(check_evidence, ensure_ascii=False)})
                final_response_only = turn == config.max_turns or concise_recovery
                # 预留最后一轮用于最终回答；生成截断后的恢复轮也禁止继续请求工具。
                schemas = [] if final_response_only else run_tools.schemas()
                messages.insert(1, {"role": "system", "content": (
                    f"Run budget: model response {turn} of {config.max_turns}. "
                    + ("This last response is reserved for your final answer. No tools are available. "
                       "Answer now using existing knowledge and observations. State unfinished work and "
                       "uncertainty honestly. Do not invent results or spend this response updating state."
                       if final_response_only else
                       "If you have enough information, answer now without more tools. "
                       "The last response is reserved for a final answer, not tool execution.")
                )})
                if concise_recovery:
                    # 上轮触及生成上限后，保持原 token 上限，要求用已有证据给出简短回答。
                    messages.insert(1, {"role": "system", "content": (
                        "The previous response reached the generation limit and was discarded; none of its tools ran. "
                        f"This request keeps the same {config.max_completion_tokens} completion-token cap. "
                        "Give a concise final answer using existing observations, preferably within 500 Chinese "
                        "characters or 250 English words. Prioritize the conclusion and key sources; "
                        "state missing evidence and unfinished work. Do not request tools or repeat the full analysis."
                    )})
                if final_response_only:
                    store.emit("final_response_requested", turn=turn, max_turns=config.max_turns)
                if dropped:
                    store.emit("context_history_limited", omitted_turns=dropped)
                request_size = len(json.dumps({"messages": messages, "tools": schemas},
                                             ensure_ascii=False))
                if request_size > config.max_input_chars:
                    raise Halt(Status.BUDGET_EXCEEDED, "input_char_limit")
                reply: ModelReply
                # 请求重试不会增加 turn，但会消耗 model_calls 和总时限。
                for attempt in range(config.max_retries + 1):
                    check()
                    if model_calls >= config.max_model_calls:
                        raise Halt(Status.BUDGET_EXCEEDED, "model_call_limit")
                    model_calls += 1
                    store.emit("model_requested", turn=turn, attempt=attempt + 1,
                               model_call=model_calls, messages=messages,
                               final_response_only=final_response_only)
                    try:
                        reply = await controlled(self.model.complete(messages, schemas),
                                                 config.request_timeout_seconds, "model_request_timeout")
                        reply = parse_reply({"choices": [{"message": reply.message(),
                                             "finish_reason": reply.finish_reason}], "usage": reply.usage}, config)
                        break
                    except ModelError as error:
                        # 格式修复进入新一轮；暂时性网络错误在本轮退避重试，二者预算独立。
                        for key, value in error.usage.items():
                            usage[key] = usage.get(key, 0) + value
                        store.emit("model_error", code=error.code, retryable=error.retryable,
                                   finish_reason=error.finish_reason, usage=error.usage,
                                   max_completion_tokens=config.max_completion_tokens)
                        if error.code == "model_completion_token_limit":
                            # 截断回复中的工具一律不执行；若已在最终回答轮，则以预算耗尽结束。
                            if final_response_only:
                                raise Halt(Status.BUDGET_EXCEEDED, "completion_token_limit") from None
                            concise_recovery = True
                            store.emit("model_concise_answer_requested", turn=turn,
                                       max_completion_tokens=config.max_completion_tokens)
                            reply = None
                            break
                        if error.code in REPAIRABLE_MODEL_ERRORS and model_repairs < config.max_model_repairs and not final_response_only:
                            model_repairs += 1
                            store.emit("model_repair_requested", code=error.code, repair=model_repairs)
                            history.append([{"role": "user", "content": "Executor rejected the model response: "
                                            + error.code + ". No tools from that response ran. Return a valid response."}])
                            reply = None
                            break
                        if not error.retryable or attempt >= config.max_retries:
                            raise
                        delay = config.retry_base_seconds * 2 ** attempt
                        store.emit("model_retry_scheduled", delay_seconds=delay)
                        await controlled(asyncio.sleep(delay), delay + 1, "retry_wait_timeout")
                if reply is None:
                    continue
                check()
                # 对所有适配器再次做边界检查，测试替身或注入实现也不能绕过限制。
                message = reply.message()
                if len(json.dumps(message, ensure_ascii=False)) > config.max_output_chars:
                    raise ModelError("model_output_limit")
                if reply.finish_reason not in {"stop", "tool_calls"}:
                    raise ModelError("model_incomplete_response")
                if len(reply.tool_calls) > config.max_tool_calls_per_turn:
                    raise ModelError("model_tool_call_limit")
                ids = [call.id for call in reply.tool_calls]
                if len(set(ids)) != len(ids) or used_ids.intersection(ids):
                    # 工具 ID 在整个运行内必须唯一，否则反馈可能绑定到错误的调用。
                    if model_repairs >= config.max_model_repairs or final_response_only:
                        raise ModelError("duplicate_tool_call_id")
                    model_repairs += 1
                    store.emit("model_repair_requested", code="duplicate_tool_call_id", repair=model_repairs)
                    history.append([{"role": "user", "content": "Duplicate tool call ID rejected; none of this response's tools ran. Use new IDs."}])
                    continue
                used_ids.update(ids)
                for key, value in reply.usage.items():
                    if type(value) is int and value >= 0:
                        usage[key] = usage.get(key, 0) + value
                store.emit("model_responded", turn=turn, message=message, usage=reply.usage)
                if final_response_only and reply.tool_calls:
                    # 即使适配器无视空工具列表仍返回调用，执行器也会拒绝，保住最终回答轮。
                    store.emit("tool_dispatch_blocked", reason="final_response_only")
                    raise Halt(Status.BUDGET_EXCEEDED, "turn_limit")
                group = [message]
                history.append(group)
                if not reply.tool_calls:
                    # 第三阶段：无工具调用代表模型提交最终回答；是否验证成功由检查决定。
                    if not reply.content or not reply.content.strip():
                        raise ModelError("model_empty_response")
                    if not checks:
                        # 没有配置检查时只能标记为未验证，不能凭模型回答标记 completed。
                        return finish(Status.UNVERIFIED, "model_finished_without_verifier", reply.content)
                    verification_attempts += 1
                    self.phase("verifying")
                    store.emit("verification_started", attempt=verification_attempts)
                    check_evidence = []
                    await verify(checks, files, dispatch, check, store, check_evidence)
                    passed = all(item["passed"] for item in check_evidence)
                    store.emit("verification_finished", attempt=verification_attempts, passed=passed, checks=check_evidence)
                    self.phase("running")
                    if passed:
                        return finish(Status.COMPLETED, "configured_checks_passed", reply.content)
                    if final_response_only or verification_attempts >= config.max_verification_attempts:
                        return finish(Status.VERIFICATION_FAILED, "configured_checks_failed", reply.content)
                    history.append([{"role": "user", "content": "Executor verification failed. Correct the work and plan if possible, "
                                    "then give a new final answer; each shell check needs new approval. Evidence: "
                                    + json.dumps(check_evidence, ensure_ascii=False)}])
                    continue
                for call in reply.tool_calls:
                    # 顺序执行本轮工具，再将对应 tool_call_id 的反馈作为同组历史交回模型。
                    result = await dispatch(call)
                    group.append({"role": "tool", "tool_call_id": call.id,
                                     "content": json.dumps(result.wire(), ensure_ascii=False)})
            raise Halt(Status.BUDGET_EXCEEDED, "turn_limit")
        except Halt as halt:
            return finish(halt.status, halt.reason)
        except ModelError as error:
            return finish(Status.FAILED, error.code)
        except asyncio.CancelledError:
            # 宿主直接取消协程时也保存终态，然后继续向上传播取消信号。
            finish(Status.CANCELLED, "task_cancelled")
            raise
        except Exception:
            # 未知异常只记录固定错误码，避免原始异常中的传输凭据进入运行记录。
            return finish(Status.FAILED, "internal_error")
