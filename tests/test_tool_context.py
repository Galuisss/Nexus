import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from starlette.requests import Request
from starlette.responses import StreamingResponse

from nexus.context_records import ContextRecords
from nexus.database import Database
from nexus.store import Store
from nexus.tool_call_protocol import apply_decision, format_tool_output, text_tokens
from nexus.tool_context import ToolContext


class ToolContextStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        directory = Path(self.directory.name)
        self.database = Database(directory)
        self.context = ToolContext(
            SimpleNamespace(
                database=self.database,
                store=Store(directory),
                sessions=SimpleNamespace(active_turns={}),
                context_records=ContextRecords(self.database),
                demo=False,
                nexus_url="http://127.0.0.1:8765",
                machine=lambda machine_id: {"transport": "local"},
                rpc=AsyncMock(),
            )
        )
        await self.context.http.aclose()
        self.state = self.context.create(
            "local", {"upstream": "https://upstream.example"}, thread="t"
        )

    async def asyncTearDown(self):
        await self.context.close()
        self.database.close()
        self.directory.cleanup()

    def request(self, streaming):
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/responses",
                "headers": [],
                "query_string": b"",
            },
            receive,
        )
        request._body = json.dumps({"stream": streaming, "input": []}).encode()
        return request

    async def test_provider_without_base_url_uses_account_default(self):
        for provider, account, expected in [
            ("custom", "chatgpt", "https://chatgpt.com/backend-api/codex"),
            ("custom", "apiKey", "https://api.openai.com/v1"),
            ("openai", "chatgpt", "https://chatgpt.com/backend-api/codex"),
        ]:
            with self.subTest(provider=provider, account=account):
                self.context.service.store.data["features"] = {"toolCallId": True}
                self.context.service.rpc.side_effect = [
                    {
                        "config": {
                            "model_provider": provider,
                            "model_providers": {
                                provider: {"base_url": None, "requires_openai_auth": True}
                            },
                        }
                    },
                    {"account": {"type": account}},
                ]
                params, token = await self.context.configure("local", "thread/start", {})
                meta = json.loads(self.context.by_token(token)["meta"])
                self.assertEqual(meta["upstream"], expected)
                self.assertEqual(params["modelProvider"], provider)

    async def test_explicit_provider_base_url_is_preserved(self):
        self.context.service.store.data["features"] = {"toolCallId": True}
        self.context.service.rpc.side_effect = [
            {
                "config": {
                    "model_provider": "custom",
                    "model_providers": {"custom": {"base_url": "https://custom.example/v1"}},
                }
            }
        ]
        _, token = await self.context.configure("local", "thread/start", {})
        self.assertEqual(
            json.loads(self.context.by_token(token)["meta"])["upstream"],
            "https://custom.example/v1",
        )
        self.context.service.rpc.assert_awaited_once()

    async def test_proxy_restores_saved_missing_upstream(self):
        state = self.context.create(
            "local", {"originProvider": "custom", "upstream": None}, thread="broken"
        )
        self.context.service.rpc.return_value = {"account": {"type": "chatgpt"}}
        requests = []

        def respond(request):
            requests.append(str(request.url))
            return httpx.Response(200, json={"output": []})

        self.context.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        for _ in range(2):
            response = await self.context.proxy(
                state["token"], "backend-api/codex/responses", self.request(False)
            )
            self.assertEqual(response.status_code, 200)
        self.assertEqual(requests, ["https://chatgpt.com/backend-api/codex/responses"] * 2)
        self.assertEqual(
            json.loads(self.context.by_token(state["token"])["meta"])["upstream"],
            "https://chatgpt.com/backend-api/codex",
        )
        self.context.service.rpc.assert_awaited_once_with("local", "account/read", {})

    async def test_native_goal_output_passes_through_without_consuming_tool_id(self):
        goal = {
            "type": "function_call_output",
            "id": "fco-goal",
            "namespace": "agent",
            "name": "goal_objective",
            "output": "Continue the optimization task.\n" * 100,
        }
        call = {"type": "function_call", "call_id": "call-a", "name": "read", "arguments": "{}"}
        output = {"type": "function_call_output", "call_id": "call-a", "output": "tool evidence"}
        items = [goal, call, output]
        for _ in range(2):
            prepared, candidates = self.context.prepare_input(
                self.context.by_token(self.state["token"]), {"input": items}, "turn-a"
            )
            self.assertEqual(prepared["input"][0], goal)
            self.assertIn("Tool output (source: r1):", prepared["input"][2]["output"])
            self.assertEqual(candidates, [])
            self.assertEqual(self.context.by_token(self.state["token"])["counter"], 1)
            self.assertEqual(self.context.context_item("local", "t", goal), goal)
        self.assertEqual(items, [goal, call, output])

    async def test_candidates_use_unmarked_original_token_threshold(self):
        items = []
        for number, tokens in enumerate([2047, 2048, 2049], 1):
            original = " x" * tokens
            self.assertEqual(text_tokens(original), tokens)
            items.extend(
                [
                    {"type": "function_call", "call_id": str(number), "name": "read"},
                    {"type": "function_call_output", "call_id": str(number), "output": original},
                ]
            )
        _, candidates = self.context.prepare_input(self.state, {"input": items}, "turn-a")
        self.assertEqual([c["source_id"] for c in candidates], ["r2", "r3"])

    async def test_stream_is_forwarded_before_completion_without_content_type(self):
        release = asyncio.Event()
        call = {"type": "function_call", "call_id": "call-a", "name": "read", "arguments": "{}"}
        first = (
            "data: " + json.dumps({"type": "response.output_item.added", "item": call}) + "\n\n"
        ).encode()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield first
                await release.wait()
                yield b'data: {"type":"response.completed"}\n\n'

        self.context.http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))
        )
        response = await asyncio.wait_for(
            self.context.proxy(self.state["token"], "responses", self.request(True)), timeout=1
        )
        self.assertIsInstance(response, StreamingResponse)
        self.assertEqual(response.headers["content-type"], "text/event-stream")
        chunks = response.body_iterator
        self.assertEqual(await anext(chunks), first.splitlines(keepends=True)[0])
        self.assertFalse(release.is_set())
        self.assertEqual(self.context.find_call(self.state["token"], "call-a")["ordinal"], 1)
        release.set()
        rest = b"".join([chunk async for chunk in chunks])
        self.assertEqual(
            first.splitlines(keepends=True)[0] + rest,
            first + b'data: {"type":"response.completed"}\n\n',
        )

    async def test_json_and_error_responses_are_not_treated_as_streams(self):
        for status, streaming in [(200, False), (429, True)]:
            with self.subTest(status=status):
                self.context.http = httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(status, json={"message": "result"})
                    )
                )
                response = await self.context.proxy(
                    self.state["token"], "responses", self.request(streaming)
                )
                self.assertNotIsInstance(response, StreamingResponse)
                self.assertEqual(response.status_code, status)
                self.assertEqual(json.loads(response.body), {"message": "result"})
                await self.context.http.aclose()

    async def test_compaction_exposes_original_while_running_and_both_results_afterward(self):
        release = asyncio.Event()
        serving = asyncio.Event()
        self.context.service.events = SimpleNamespace(publish=AsyncMock())
        original = "Original tool evidence.\n" * 1024
        payload, candidates = self.context.prepare_input(
            self.state,
            {
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call-a",
                        "name": "read",
                        "arguments": "{}",
                    },
                    {"type": "function_call_output", "call_id": "call-a", "output": original},
                ]
            },
            "turn-a",
        )
        decision = {"r1": {"mode": "summary", "text": "Retained tool evidence."}}

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                serving.set()
                await release.wait()
                for event in [
                    {"type": "response.output_text.delta", "delta": json.dumps(decision)},
                    {"type": "response.completed", "response": {"usage": {}}},
                ]:
                    yield ("data: " + json.dumps(event) + "\n\n").encode()

        self.context.http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))
        )
        task = asyncio.create_task(
            self.context.compact(
                self.state, payload, candidates, {}, "https://upstream.example", "turn-a"
            )
        )
        await asyncio.wait_for(serving.wait(), timeout=1)
        started = self.context.service.events.publish.await_args_list[0].args[2]["item"]
        self.assertEqual(started["results"][0]["call"], payload["input"][0])
        self.assertEqual(started["results"][0]["original"]["output"], original)
        self.assertNotIn("projected", started["results"][0])
        turns = [{"id": "turn-a", "items": []}]
        self.context.annotate("local", "t", turns)
        self.assertEqual(turns[0]["items"][0], started)

        release.set()
        projected = await asyncio.wait_for(task, timeout=1)
        completed = self.context.service.events.publish.await_args_list[-1].args[2]["item"]
        result = completed["results"][0]
        self.assertEqual(result["call"], payload["input"][0])
        self.assertEqual(result["original"]["output"], original)
        self.assertEqual(result["outcome"], "compacted")
        self.assertEqual(result["projected"], projected["input"][-1])
        self.assertIn("Retained tool evidence.", result["projected"]["output"])
        turns = [{"id": "turn-a", "items": []}]
        self.context.annotate("local", "t", turns)
        self.assertEqual(turns[0]["items"][0], completed)

    async def test_failed_compaction_keeps_original_without_projected_result(self):
        self.context.service.events = SimpleNamespace(publish=AsyncMock())
        original = "Original evidence.\n" * 1024
        payload, candidates = self.context.prepare_input(
            self.state,
            {
                "input": [
                    {
                        "type": "custom_tool_call",
                        "call_id": "call-a",
                        "name": "read",
                        "input": "read()",
                    },
                    {
                        "type": "custom_tool_call_output",
                        "call_id": "call-a",
                        "output": [{"type": "input_text", "text": original}],
                    },
                ]
            },
            "turn-a",
        )
        self.context.http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(503))
        )
        await self.context.compact(
            self.state, payload, candidates, {}, "https://upstream.example", "turn-a"
        )
        completed = self.context.service.events.publish.await_args_list[-1].args[2]["item"]
        self.assertEqual(completed["status"], "failed")
        self.assertEqual(completed["results"][0]["call"]["input"], "read()")
        self.assertEqual(completed["results"][0]["original"]["output"][0]["text"], original)
        self.assertNotIn("projected", completed["results"][0])
        self.assertEqual(completed["results"][0]["outcome"], "fallback_failed")

    async def test_keep_growth_and_failure_expose_only_original(self):
        self.context.service.events = SimpleNamespace(publish=AsyncMock())
        items = []
        for number in range(1, 6):
            items.extend(
                [
                    {"type": "function_call", "call_id": str(number), "name": "read"},
                    {"type": "function_call_output", "call_id": str(number), "output": " x" * 2048},
                ]
            )
        payload, candidates = self.context.prepare_input(self.state, {"input": items}, "turn-a")
        decision = {
            "r1": {"mode": "keep"},
            "r2": {"mode": "summary", "text": " y" * 4096},
            "r3": {"mode": "extract", "spans": [["missing", "anchor"]]},
            "r5": {"mode": "summary", "text": "x"},
        }

        def respond(request):
            query = json.loads(request.content)
            self.assertIn(
                "Retain facts found in that return only", query["input"][-2]["content"][0]["text"]
            )
            self.assertEqual(
                json.loads(query["input"][-1]["content"][0]["text"]), ["r1", "r2", "r3", "r4", "r5"]
            )
            events = [
                {"type": "response.output_text.delta", "delta": json.dumps(decision)},
                {"type": "response.completed", "response": {"usage": {}}},
            ]
            return httpx.Response(
                200, content="".join("data: " + json.dumps(e) + "\n\n" for e in events)
            )

        self.context.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        projected = await self.context.compact(
            self.state, payload, candidates, {}, "https://upstream.example", "turn-a"
        )
        completed = self.context.service.events.publish.await_args_list[-1].args[2]["item"]
        self.assertEqual(
            [r["outcome"] for r in completed["results"]],
            ["keep", "fallback_growth", "fallback_failed", "fallback_failed", "compacted"],
        )
        for result in completed["results"][:4]:
            self.assertNotIn("projected", result)
        for original, retained in zip(payload["input"][:8], projected["input"][:8]):
            self.assertEqual(original, retained)
        self.assertEqual(completed["results"][-1]["projected"], projected["input"][-1])
        turns = [{"id": "turn-a", "items": []}]
        self.context.annotate("local", "t", turns)
        self.assertEqual(turns[0]["items"][0], completed)


class ToolCallProjectionTests(unittest.TestCase):
    def test_growth_including_labels_returns_marked_original(self):
        original = {"output": " x" * 2048}
        decision = {"mode": "summary", "text": " x" * 2040}
        self.assertLess(text_tokens(decision["text"]), text_tokens(original["output"]))
        projected, diagnostic = apply_decision(original, "r1", decision)
        self.assertEqual(projected, format_tool_output(original, "r1"))
        self.assertEqual(diagnostic["type"], "token_growth")
        self.assertGreater(diagnostic["projected_tokens"], diagnostic["original_tokens"])
