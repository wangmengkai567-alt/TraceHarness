from __future__ import annotations

import json
from typing import Protocol

import httpx

from .config import Config
from .contracts import ModelError, ModelReply, ToolCall


class Model(Protocol):
    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply: ...


def parse_reply(payload: dict, config: Config) -> ModelReply:
    try:
        if not isinstance(payload, dict):
            raise ValueError
        choice = payload["choices"][0]
        message = choice["message"]
        if message.get("role") != "assistant":
            raise ValueError
        reason = choice["finish_reason"]
        if reason not in {"stop", "tool_calls"}:
            raise ModelError("model_incomplete_response")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ValueError
        if len(raw_calls) > config.max_tool_calls_per_turn:
            raise ModelError("model_tool_call_limit")
        calls = []
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
        usage = payload.get("usage") or {}
        if not isinstance(usage, dict):
            raise ValueError
        usage = {key: value for key, value in usage.items()
                 if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                 and type(value) is int and value >= 0}
        return ModelReply(content, tuple(calls), usage, reason)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise ModelError("model_invalid_response") from exc


class OpenAICompatibleModel:
    """Chat Completions tool protocol; no provider-specific model names."""

    def __init__(self, config: Config, api_key: str,
                 transport: httpx.AsyncBaseTransport | None = None):
        if not api_key.strip():
            raise ValueError("set HARNESS_API_KEY in local environment or an explicit env file")
        self.config = config
        self.api_key = api_key
        self.transport = transport

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        payload = {"model": self.config.model, "messages": messages, "stream": False,
                   self.config.completion_token_parameter: self.config.max_completion_tokens}
        # Omitting tools supports providers that reject an empty tools array.
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
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > self.config.max_response_bytes:
                            raise ModelError("model_response_byte_limit")
                        body.extend(chunk)
            return parse_reply(json.loads(body), self.config)
        except httpx.TimeoutException as exc:
            raise ModelError("model_request_timeout", retryable=True) from exc
        except httpx.TransportError as exc:
            # Provider URLs, headers, and raw bodies are intentionally not logged.
            raise ModelError("model_transport_error", retryable=True) from exc
        except (ValueError, RecursionError) as exc:
            raise ModelError("model_invalid_json") from exc
