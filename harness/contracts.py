"""模块之间共用的数据约定：运行状态、模型回复、工具调用及安全错误。

内部用数据类传递信息，wire/message 方法再转换为模型协议或日志所需的字典。
工具名、状态值和错误码是程序协议字段，保留英文以保持各模块兼容。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Status(str, Enum):
    # completed 仅表示配置的检查全部通过；模型给出了答复但无检查时为 unverified。
    COMPLETED = "completed"  # 已配置的检查全部通过。
    VERIFICATION_FAILED = "verification_failed"  # 配置检查未全部通过，且不能继续修正。
    INTERRUPTED = "interrupted"  # 历史记录缺少终态，运行已中断。
    UNVERIFIED = "unverified"  # 已给出回答，但没有独立检查证明完成。
    FAILED = "failed"  # 模型、工具或内部控制遇到无法继续的错误。
    CANCELLED = "cancelled"  # 用户或宿主取消任务。
    BUDGET_EXCEEDED = "budget_exceeded"  # 时间、轮次或调用次数等预算耗尽。


@dataclass(frozen=True)
class ToolCall:
    # 模型提出的一次调用；arguments 保留原始 JSON 字符串，由具体工具验证参数。
    id: str  # 关联工具请求与反馈的唯一 ID。
    name: str  # 待调用的工具名称。
    arguments: str  # 模型提交的 JSON 参数文本，尚未经业务校验。

    def wire(self) -> dict:
        # 按 Chat Completions 的 function 工具调用格式序列化。
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": self.arguments}}


@dataclass(frozen=True)
class ModelReply:
    # 一次模型响应可以包含文本、多个工具调用，以及本次请求的 token 用量。
    content: str | None = None  # 模型文本回复，纯工具调用时允许为空。
    tool_calls: tuple[ToolCall, ...] = ()  # 本轮需要按顺序执行的工具调用。
    usage: dict[str, int] = field(default_factory=dict)  # 服务商返回的 token 用量。
    finish_reason: str = "stop"  # 正常结束、请求工具或其他服务商结束原因。

    def message(self) -> dict:
        # 恢复为 assistant 消息，后续会和工具执行结果一起放入对话历史。
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [call.wire() for call in self.tool_calls]
        return message


@dataclass(frozen=True)
class ToolResult:
    # ok 表示本次工具是否成功；truncated 提醒调用方输出不完整，避免误判证据。
    ok: bool  # 本次操作是否成功，不等于整个任务已通过验收。
    data: Any = None  # 工具返回的数据或可观察证据。
    error: str | None = None  # 失败时使用的固定错误码。
    truncated: bool = False  # 返回数据是否因预算限制而不完整。

    def wire(self) -> dict:
        # 工具反馈和事件日志共用这一结构，失败时通过 error 返回稳定的错误码。
        return {"ok": self.ok, "data": self.data,
                "error": self.error, "truncated": self.truncated}


class ModelError(Exception):
    """只携带安全错误码，不携带模型服务商的原始响应体或请求头。"""

    def __init__(self, code: str, *, retryable: bool = False,
                 finish_reason: str | None = None, usage: dict | None = None):
        # retryable 交由 Runner 决定是否重试；只保留允许的结束原因和有效用量。
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.finish_reason = (finish_reason if finish_reason in {None, "length", "content_filter", "stop", "tool_calls"}
                              else "unknown")
        self.usage = {key: value for key, value in (usage or {}).items()
                      if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                      and type(value) is int and value >= 0}
