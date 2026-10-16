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
from .tools import CoreTools, bound_result
from .files import FileError, FileTools
from .context import RunTools, TaskContext
from .processes import TerminationUnconfirmed
from .shell import ShellTools
from .recovery import Recovery, REPAIRABLE_MODEL_ERRORS
from .verification import verify

T = TypeVar("T")
LIVE_CHECK_TASK = "先调用 add_integers 计算 47+58，再把工具返回值交给 multiply_integers 乘以 3，报告最终结果。"


class Tools(Protocol):
    def schemas(self) -> list[dict]: ...
    async def execute(self, call: ToolCall) -> ToolResult: ...


class Halt(Exception):
    def __init__(self, status: Status, reason: str):
        self.status, self.reason = status, reason


class Runner:
    def __init__(self, config: Config, model: Model, *, tools: Tools | None = None,
                 secrets: tuple[str, ...] = (), approve=None, phase=None):
        config.validate()
        self.config, self.model = config, model
        self.tools = tools if tools is not None else CoreTools()
        self.secrets = secrets
        self.approve = approve
        self.phase = phase if phase is not None else lambda value: None
        self.termination_unconfirmed = False

    async def run(self, task: str, workspace: Path,
                  cancel: asyncio.Event | None = None, *, store: RunStore | None = None) -> dict:
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must not be empty")
        workspace = workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("workspace must be a directory")
        cancel = cancel if cancel is not None else asyncio.Event()
        config = self.config
        store = store if store is not None else RunStore(Path(config.runs_dir).resolve(), self.secrets)
        started = time.monotonic()
        deadline = started + config.total_timeout_seconds
        turns = 0
        model_calls = 0
        tool_calls = 0
        usage: dict[str, int] = {}
        used_ids: set[str] = set()
        history: list[list[dict]] = []
        context = None
        run_tools = self.tools
        recovery = None
        operations = []
        artifacts = {}
        check_evidence = []
        verification_attempts = 0
        model_repairs = 0
        checks = copy.deepcopy(config.checks)
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
            if cancel.is_set():
                raise Halt(Status.CANCELLED, "user_cancelled")
            if time.monotonic() >= deadline:
                raise Halt(Status.BUDGET_EXCEEDED, "total_timeout")

        async def controlled(operation: Awaitable[T], timeout: float, code: str) -> T:
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
                for pending in (job, cancelled):
                    if not pending.done():
                        pending.cancel()
                outcomes = await asyncio.gather(job, cancelled, return_exceptions=True)
                if any(isinstance(outcome, TerminationUnconfirmed) for outcome in outcomes):
                    self.termination_unconfirmed = True
                    raise Halt(Status.FAILED, "process_termination_unconfirmed")

        def finish(status: Status, reason: str, answer: str | None = None) -> dict:
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
                "completion_scope": "Only configured checks are verified." if status == Status.COMPLETED else
                                    "No verified completion; consult checks and unfinished work.",
            })
            store.emit("run_finished", **result)
            store.write_json("result.json", result)
            return result

        async def dispatch(call: ToolCall, purpose="task") -> ToolResult:
            nonlocal tool_calls
            check()
            if tool_calls >= config.max_tool_calls:
                raise Halt(Status.BUDGET_EXCEEDED, "tool_call_limit")
            tool_calls += 1
            store.emit("tool_requested", call_id=call.id, name=call.name, arguments=call.arguments, purpose=purpose)
            record = {"call_id": call.id, "name": call.name, "purpose": purpose,
                      "ok": None, "error": "operation_interrupted_or_outcome_unknown"}
            operations.append(record)
            result = recovery.blocked(call)
            # A configured check is explicitly repeatable, but still needs a fresh shell approval.
            if purpose == "verification" and result and result.error == "side_effect_uncertain_no_replay":
                result = None
            if result is None:
                try:
                    timeout = (config.approval_timeout_seconds + config.shell_timeout_seconds + 1
                               if call.name == "run_command" and self.approve else config.tool_timeout_seconds)
                    result = await controlled(run_tools.execute(call), timeout, "tool_timeout")
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
            core = (ShellTools(workspace, config, self.approve, store.emit, cancel, self.tools)
                    if self.approve else self.tools)
            run_tools = RunTools(core, files, context)
            recovery = Recovery(files)
            for turn in range(1, config.max_turns + 1):
                check()
                turns = turn
                messages, dropped = context.assemble(history, config.recent_history_turns)
                if checks:
                    messages.insert(1, {"role": "system", "content": "Operator-configured verification checks: "
                        + json.dumps(checks, ensure_ascii=False) + ". On your final answer the executor runs these checks. "
                        "If they fail, repair within remaining budgets and answer again. Each shell check needs approval. "
                        "Previous check evidence: " + json.dumps(check_evidence, ensure_ascii=False)})
                final_response_only = turn == config.max_turns
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
                if final_response_only:
                    store.emit("final_response_requested", turn=turn, max_turns=config.max_turns)
                if dropped:
                    store.emit("context_history_limited", omitted_turns=dropped)
                request_size = len(json.dumps({"messages": messages, "tools": schemas},
                                             ensure_ascii=False))
                if request_size > config.max_input_chars:
                    raise Halt(Status.BUDGET_EXCEEDED, "input_char_limit")
                reply: ModelReply
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
                        store.emit("model_error", code=error.code, retryable=error.retryable)
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
                # Protect every adapter, including injected/simulated ones.
                message = reply.message()
                if len(json.dumps(message, ensure_ascii=False)) > config.max_output_chars:
                    raise ModelError("model_output_limit")
                if reply.finish_reason not in {"stop", "tool_calls"}:
                    raise ModelError("model_incomplete_response")
                if len(reply.tool_calls) > config.max_tool_calls_per_turn:
                    raise ModelError("model_tool_call_limit")
                ids = [call.id for call in reply.tool_calls]
                if len(set(ids)) != len(ids) or used_ids.intersection(ids):
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
                    # Enforce the reserved response even for an adapter that ignores its tool list.
                    store.emit("tool_dispatch_blocked", reason="final_response_only")
                    raise Halt(Status.BUDGET_EXCEEDED, "turn_limit")
                group = [message]
                history.append(group)
                if not reply.tool_calls:
                    if not reply.content or not reply.content.strip():
                        raise ModelError("model_empty_response")
                    if not checks:
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
                    result = await dispatch(call)
                    group.append({"role": "tool", "tool_call_id": call.id,
                                     "content": json.dumps(result.wire(), ensure_ascii=False)})
            raise Halt(Status.BUDGET_EXCEEDED, "turn_limit")
        except Halt as halt:
            return finish(halt.status, halt.reason)
        except ModelError as error:
            return finish(Status.FAILED, error.code)
        except asyncio.CancelledError:
            # Preserve a terminal record when the embedding application cancels the task.
            finish(Status.CANCELLED, "task_cancelled")
            raise
        except Exception:
            # Never persist a raw exception which might contain transport credentials.
            return finish(Status.FAILED, "internal_error")
