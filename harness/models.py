"""兼容 Chat Completions 的模型适配层：发送请求并严格解析结构化回复。

适配层只负责单次请求；重试、轮次控制、工具执行和失败恢复由 Runner 管理。
外部响应必须先经过 parse_reply 校验，才能进入工具执行循环。
"""
from __future__ import annotations

import json
from typing import Protocol

import httpx

from .config import Config
from .contracts import ModelError, ModelReply, ToolCall


class Model(Protocol):
    # 结构化接口约定，测试可以注入实现相同 complete 方法的模型替身。
    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply: ...


def parse_reply(payload: dict, config: Config) -> ModelReply:
    # 仅接受第一条 assistant 回复，规范化用量，并拒绝不完整或格式非法的工具调用。
    try:
        if not isinstance(payload, dict):
            raise ValueError
        choice = payload["choices"][0]
        message = choice["message"]
        if message.get("role") != "assistant":
            raise ValueError
        usage = payload.get("usage") or {}
        if not isinstance(usage, dict):
            raise ValueError
        usage = {key: value for key, value in usage.items()
                 if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                 and type(value) is int and value >= 0}
        reason = choice["finish_reason"]
        # length 表示生成被截断，不能把残缺文本或参数交给工具；用量仍随错误返回。
        if reason == "length":
            raise ModelError("model_completion_token_limit", finish_reason=reason, usage=usage)
        if reason == "content_filter":
            raise ModelError("model_content_filtered", finish_reason=reason, usage=usage)
        if reason not in {"stop", "tool_calls"}:
            raise ModelError("model_incomplete_response", finish_reason="unknown", usage=usage)
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ValueError
        if len(raw_calls) > config.max_tool_calls_per_turn:
            raise ModelError("model_tool_call_limit")
        calls = []
        # arguments 暂不解析为具体参数；这里只验证协议，业务参数交给对应工具校验。
        for item in raw_calls:
            if item["type"] != "function":
                raise ValueError
            function = item["function"]
            call = ToolCall(item["id"], function["name"], function["arguments"])
            if not all(isinstance(value, str) and value for value in (call.id, call.name, call.arguments)):
                raise ValueError
            calls.append(call)
        if len({call.id for call in calls}) != len(calls):
            raise ModelError("duplicate_tool_call_id")
        output_size = len(json.dumps(message, ensure_ascii=False))
        if output_size > config.max_output_chars:
            raise ModelError("model_output_limit")
        if reason == "tool_calls" and not calls:
            raise ValueError
        if not calls and not (content and content.strip()):
            raise ModelError("model_empty_response")
        return ModelReply(content, tuple(calls), usage, reason)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise ModelError("model_invalid_response") from exc


class OpenAICompatibleModel:
    """实现 Chat Completions 工具协议，模型名由配置指定，不硬编码服务商型号。"""

    def __init__(self, config: Config, api_key: str,
                 transport: httpx.AsyncBaseTransport | None = None):
        # transport 可用于测试替换 HTTP 通道；生产调用使用 httpx 默认传输。
        if not api_key.strip():
            raise ValueError("set HARNESS_API_KEY in local environment or an explicit env file")
        self.config = config
        self.api_key = api_key
        self.transport = transport

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        # 通过可配置字段兼容 max_tokens / max_completion_tokens，单次生成上限保持一致。
        payload = {"model": self.config.model, "messages": messages, "stream": False,
                   self.config.completion_token_parameter: self.config.max_completion_tokens}
        # 没有可用工具时省略 tools 字段，兼容不接受空工具数组的服务商。
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        try:
            async with httpx.AsyncClient(timeout=self.config.request_timeout_seconds,
                                         follow_redirects=False,
                                         transport=self.transport) as client:
                async with client.stream(
                    "POST", self.config.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"}, json=payload,
                ) as response:
                    if response.status_code != 200:
                        code = response.status_code
                        raise ModelError(f"model_http_{code}",
                                         retryable=code in {408, 409, 429, 500, 502, 503, 504})
                    body = bytearray()
                    # 按块读取并先检查原始字节上限，避免将任意大的响应载入内存。
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > self.config.max_response_bytes:
                            raise ModelError("model_response_byte_limit")
                        body.extend(chunk)
            return parse_reply(json.loads(body), self.config)
        except httpx.TimeoutException as exc:
            raise ModelError("model_request_timeout", retryable=True) from exc
        except (httpx.UnsupportedProtocol, httpx.LocalProtocolError) as exc:
            raise ModelError("model_transport_configuration_error") from exc
        except httpx.TransportError as exc:
            # 不把可能含凭据的服务商 URL、请求头和原始响应体写入错误或日志。
            raise ModelError("model_transport_error", retryable=True) from exc
        except (ValueError, RecursionError) as exc:
            raise ModelError("model_invalid_json") from exc
