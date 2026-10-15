"""Real loopback HTTP transport; responses are simulated, not a real LLM."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from harness.cli import check_live_evidence
from harness.config import Config
from harness.models import OpenAICompatibleModel
from tests.fakes import ToolLoopFake
from harness.runner import LIVE_CHECK_TASK, Runner


class HTTPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_socket_loop_retry_and_feedback(self):
        requests = []
        errors = []
        demo = ToolLoopFake()

        async def handle(reader, writer):
            try:
                header = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
                lines = header.split("\r\n")
                if lines[0] != "POST /v1/chat/completions HTTP/1.1":
                    raise AssertionError("wrong endpoint")
                lengths = [int(line.split(":", 1)[1]) for line in lines if line.lower().startswith("content-length:")]
                payload = json.loads(await reader.readexactly(lengths[0]))
                requests.append(payload)
                if len(requests) == 1:
                    status, body = "429 Too Many Requests", b'{"error":"simulated transient failure"}'
                else:
                    reply = await demo.complete(payload["messages"], payload["tools"])
                    status = "200 OK"
                    body = json.dumps({"choices": [{"message": reply.message(),
                                                    "finish_reason": reply.finish_reason}],
                                       "usage": {"total_tokens": 10}}, ensure_ascii=False).encode("utf-8")
                writer.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii") + body)
                await writer.drain()
            except Exception as error:
                errors.append(type(error).__name__)
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(provider="openai_compatible", model="local-simulated-model",
                            base_url=f"http://127.0.0.1:{port}/v1", runs_dir=str(Path(tmp) / "runs"),
                            retry_base_seconds=0.01)
            model = OpenAICompatibleModel(config, "dummy-local-key")
            result = await Runner(config, model, secrets=("dummy-local-key",)).run(LIVE_CHECK_TASK, Path(tmp))
            self.assertEqual(errors, [])
            self.assertEqual((result["status"], result["model_calls"], result["tool_calls"]), ("unverified", 4, 2))
            self.assertTrue(check_live_evidence(result))
            self.assertEqual(result["usage"], {"total_tokens": 30})
            self.assertEqual(requests[0], requests[1])
            self.assertEqual(requests[-1]["messages"][-1]["role"], "tool")

    async def test_cancellation_closes_real_http_connection(self):
        entered, closed = asyncio.Event(), asyncio.Event()

        async def handle(reader, writer):
            try:
                header = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
                length = next(int(line.split(":", 1)[1]) for line in header.split("\r\n")
                              if line.lower().startswith("content-length:"))
                await reader.readexactly(length)
                entered.set()
                await reader.read()  # No response: wait for client cancellation/close.
            finally:
                writer.close()
                await writer.wait_closed()
                closed.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(provider="openai_compatible", model="local-simulated-model",
                            base_url=f"http://127.0.0.1:{port}/v1", runs_dir=str(Path(tmp) / "runs"))
            cancel = asyncio.Event()
            task = asyncio.create_task(Runner(config, OpenAICompatibleModel(config, "dummy-key"))
                                       .run(LIVE_CHECK_TASK, Path(tmp), cancel))
            await asyncio.wait_for(entered.wait(), 5)
            cancel.set()
            result = await asyncio.wait_for(task, 5)
            await asyncio.wait_for(closed.wait(), 5)
            self.assertEqual(result["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
