import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from mcp.server.mcpserver import Context

from nexus.mcp_server import NexusAPI, create_mcp_server


class MessageToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_sender_comes_from_request_metadata_and_is_not_a_model_argument(self):
        server = create_mcp_server("http://nexus.test")
        tools = {tool.name: tool for tool in await server.list_tools()}
        properties = tools["send_message"].input_schema["properties"]
        self.assertNotIn("ctx", properties)
        self.assertNotIn("sender_id", properties)
        context = Context(
            request_context=SimpleNamespace(meta={"threadId": "caller-thread"}), mcp_server=server
        )
        with patch.object(
            NexusAPI, "request", new_callable=AsyncMock, return_value={"turnId": "turn"}
        ) as request:
            result = await server.call_tool(
                "send_message", {"thread_id": "abc123", "text": "hello"}, context=context
            )
            self.assertFalse(result.is_error)
            self.assertEqual(request.await_args.args, ("POST", "/api/messages"))
            body = request.await_args.kwargs["json"]
            self.assertEqual(body["senderThreadId"], "caller-thread")
            self.assertEqual(body["threadId"], "abc123")
            self.assertNotIn("machineId", body)


class ScreenshotToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_screenshot_returns_native_image_content(self):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aC4sAAAAASUVORK5CYII="
        )
        server = create_mcp_server("http://nexus.test")
        response = httpx.Response(
            200,
            content=png,
            headers={
                "X-Nexus-Screenshot-Path": "/home/user/.cache/nexus/screenshots/screenshot.png",
            },
        )
        with patch.object(NexusAPI, "request", new_callable=AsyncMock, return_value=response):
            for arguments, include_path in (
                ({"machine_id": "phone"}, True),
                ({"machine_id": "phone", "include_path": False}, False),
            ):
                with self.subTest(include_path=include_path):
                    result = await server.call_tool("capture_screenshot", arguments)
                    self.assertFalse(result.is_error)
                    self.assertIsNone(result.structured_content)
                    self.assertEqual(len(result.content), 2 if include_path else 1)
                    content = result.content[0]
                    self.assertEqual(content.type, "image")
                    self.assertEqual(content.mime_type, "image/png")
                    self.assertEqual(base64.b64decode(content.data), png)
                    if include_path:
                        self.assertIn(
                            response.headers["X-Nexus-Screenshot-Path"], result.content[1].text
                        )


class ProjectToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_tools_use_existing_http_endpoints(self):
        server = create_mcp_server("http://nexus.test")
        tools = {tool.name: tool for tool in await server.list_tools()}
        machine = {"id": "machine id", "projects": ["/project"], "status": "connected"}
        with patch.object(
            NexusAPI, "request", new_callable=AsyncMock, return_value=machine
        ) as request:
            for name, method in (("add_project", "POST"), ("remove_project", "DELETE")):
                with self.subTest(tool=name):
                    self.assertEqual(tools[name].input_schema["required"], ["machine_id", "cwd"])
                    result = await server.call_tool(
                        name, {"machine_id": "machine id", "cwd": "/project"}
                    )
                    self.assertFalse(result.is_error)
                    self.assertEqual(result.structured_content, machine)
                    request.assert_awaited_with(
                        method, "/api/machines/machine%20id/projects", json={"cwd": "/project"}
                    )


class HistoryToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_and_search_use_one_archive_status_parameter(self):
        server = create_mcp_server("http://nexus.test")
        tools = {tool.name: tool for tool in await server.list_tools()}
        with patch.object(
            NexusAPI, "request", new_callable=AsyncMock, return_value={"data": []}
        ) as request:
            for name in ("list_threads", "search_threads"):
                properties = tools[name].input_schema["properties"]
                self.assertNotIn("archived", properties)
                self.assertEqual(
                    properties["archive_status"]["enum"], ["all", "unarchived", "archived"]
                )
                self.assertEqual(properties["archive_status"]["default"], "all")
                if name == "list_threads":
                    self.assertEqual(properties["scope"]["enum"], ["history", "loaded"])
                    self.assertEqual(properties["scope"]["default"], "history")
                for status, native in (
                    (None, None),
                    ("all", None),
                    ("unarchived", "false"),
                    ("archived", "true"),
                ):
                    with self.subTest(tool=name, status=status):
                        arguments = {"machine_id": "machine", "search_term": "topic", "limit": 2}
                        if status is not None:
                            arguments["archive_status"] = status
                        result = await server.call_tool(name, arguments)
                        self.assertFalse(result.is_error)
                        params = request.call_args.kwargs["params"]
                        self.assertEqual(params.get("archived"), native)
                        if name == "list_threads":
                            self.assertEqual(params["scope"], "history")
                            self.assertEqual(request.call_args.args, ("GET", "/api/threads"))
                            result = await server.call_tool(
                                name, {**arguments, "cursor": "next-page"}
                            )
                            self.assertFalse(result.is_error)
                            self.assertEqual(
                                request.call_args.args, ("GET", "/api/machines/machine/threads")
                            )
                            self.assertEqual(
                                request.call_args.kwargs["params"],
                                {
                                    key: value
                                    for key, value in {**params, "cursor": "next-page"}.items()
                                    if key != "machineId"
                                },
                            )
                            result = await server.call_tool(name, {**arguments, "scope": "loaded"})
                            self.assertFalse(result.is_error)
                            self.assertEqual(
                                request.call_args.kwargs["params"], {**params, "scope": "loaded"}
                            )
