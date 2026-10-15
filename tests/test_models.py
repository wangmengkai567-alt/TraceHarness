import asyncio
import json
import unittest
from dataclasses import replace

import httpx

from harness.config import Config
from harness.contracts import ModelError
from harness.models import OpenAICompatibleModel, parse_reply


def response(content="done", *, tools=None, finish="stop"):
    message = {"role": "assistant", "content": content}
    if tools is not None:
        message["tool_calls"] = tools
    return {"choices": [{"message": message, "finish_reason": finish}],
            "usage": {"total_tokens": 7}}


class ModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = Config(provider="openai_compatible", model="test-model", base_url="https://api.openai.com/v1")

    async def test_request_contract_and_parse(self):
        async def handler(request):
            self.assertEqual(str(request.url), "https://api.openai.com/v1/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer test-key")
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "test-model")
            self.assertEqual(payload["max_tokens"], 1024)
            self.assertFalse(payload["stream"])
            self.assertNotIn("max_completion_tokens", payload)
            return httpx.Response(200, json=response())
        model = OpenAICompatibleModel(self.config, "test-key", httpx.MockTransport(handler))
        reply = await model.complete([{"role": "user", "content": "hello"}], [])
        self.assertEqual(reply.content, "done")
        self.assertEqual(reply.usage, {"total_tokens": 7})

    async def test_http_error_classification_no_raw_body(self):
        for status, retryable in ((401, False), (400, False), (429, True), (503, True), (302, False)):
            async def handler(request, status=status):
                return httpx.Response(status, text="sensitive-provider-body-and-key")
            model = OpenAICompatibleModel(self.config, "test-key", httpx.MockTransport(handler))
            with self.assertRaises(ModelError) as caught:
                await model.complete([], [])
            self.assertEqual(caught.exception.retryable, retryable)
            self.assertEqual(str(caught.exception), f"model_http_{status}")

    async def test_tool_free_request_omits_tools_and_tool_choice(self):
        async def handler(request):
            payload = json.loads(request.content)
            self.assertNotIn("tools", payload)
            self.assertNotIn("tool_choice", payload)
            return httpx.Response(200, json=response("final answer"))
        model = OpenAICompatibleModel(self.config, "test-key", httpx.MockTransport(handler))
        self.assertEqual((await model.complete([{"role": "user", "content": "answer"}], [])).content,
                         "final answer")

    async def test_transport_timeout_is_retryable(self):
        async def handler(request):
            raise httpx.ReadTimeout("potential secret", request=request)
        model = OpenAICompatibleModel(self.config, "test-key", httpx.MockTransport(handler))
        with self.assertRaises(ModelError) as caught:
            await model.complete([], [])
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(str(caught.exception), "model_request_timeout")

    async def test_response_byte_limit(self):
        async def handler(request):
            return httpx.Response(200, content=b"x" * 1001)
        model = OpenAICompatibleModel(replace(self.config, max_response_bytes=1000),
                                      "test-key", httpx.MockTransport(handler))
        with self.assertRaises(ModelError) as caught:
            await model.complete([], [])
        self.assertEqual(str(caught.exception), "model_response_byte_limit")

    async def test_invalid_json(self):
        async def handler(request):
            return httpx.Response(200, content=b"not json")
        model = OpenAICompatibleModel(self.config, "test-key", httpx.MockTransport(handler))
        with self.assertRaises(ModelError) as caught:
            await model.complete([], [])
        self.assertEqual(str(caught.exception), "model_invalid_json")

    def test_invalid_response_shapes(self):
        for payload in ({}, [], {"choices": []}, response(content="", finish="length"), response(content="")):
            with self.assertRaises(ModelError):
                parse_reply(payload, self.config)

    def test_tool_calls_parse_and_limits(self):
        tool = {"id": "call1", "type": "function", "function": {
            "name": "add_integers", "arguments": '{"a":47,"b":58}'}}
        reply = parse_reply(response(None, tools=[tool], finish="tool_calls"), self.config)
        self.assertEqual(reply.tool_calls[0].name, "add_integers")
        with self.assertRaises(ModelError):
            parse_reply(response(None, tools=[tool, tool], finish="tool_calls"), self.config)
        with self.assertRaises(ModelError):
            parse_reply(response(None, tools=[tool] * 5, finish="tool_calls"), self.config)


if __name__ == "__main__":
    unittest.main()
