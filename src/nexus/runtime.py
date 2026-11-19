"""Pinned Codex Python SDK adapter for local and SSH app-server transports."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import sys
from contextlib import suppress
from queue import Empty
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from openai_codex._message_router import MessageRouter
from openai_codex.client import CodexClient, CodexConfig
from openai_codex.errors import CodexError, JsonRpcError, TransportClosedError
from openai_codex.models import Notification, UnknownNotification
from pydantic import RootModel

from .errors import NexusError
from .network import REVERSE_PORT
from .ssh import SSH_OPTIONS, ssh_target
from .titles import TITLE_MODEL, TITLE_SCHEMA, parse_title, title_config, title_prompt


class RpcResult(RootModel[dict[str, Any]]):
    pass


_DEFERRED = object()
RPC_TIMEOUT = 30
CLOSE_TIMEOUT = 5
logger = logging.getLogger(__name__)


class GatewayMessageRouter(MessageRouter):
    """Deliver all observed turns to the gateway's single event consumer.

    The SDK normally partitions turn-scoped events for its run()/turn()
    consumers. A gateway uses raw RPCs and needs that entire stream instead.
    """

    def __init__(self, on_notification=None, on_disconnect=None):
        super().__init__()
        self.on_notification = on_notification
        self.on_disconnect = on_disconnect
        self.response_callbacks: dict[str, Callable] = {}

    def route_notification(self, notification: Notification) -> None:
        if self.on_notification:
            self.on_notification(notification.method, notification.payload.params)
        else:
            self._global_notifications.put(notification)

    def route_response(self, message) -> None:
        with self._lock:
            callback = self.response_callbacks.pop(str(message.get("id")), None)
        if callback and "result" in message:
            callback(message["result"])
        super().route_response(message)

    def fail_all(self, error) -> None:
        super().fail_all(error)
        if self.on_disconnect:
            self.on_disconnect(str(error))


class InteractiveCodexClient(CodexClient):
    """Leave server requests pending while the SDK reader continues routing RPCs.

    Codex SDK 0.169.0 normally calls an approval handler synchronously on its
    reader thread. These two hooks defer just the reply; the SDK still owns
    transport, framing, request correlation, and notification routing.
    """

    def __init__(
        self,
        config: CodexConfig,
        on_request: Callable[[dict], None],
        on_timeout: Callable[[str], None] | None = None,
        on_notification: Callable[[str, dict], None] | None = None,
    ):
        super().__init__(config)
        self._router = GatewayMessageRouter(on_notification, on_timeout)
        self.on_request = on_request
        self.on_timeout = on_timeout

    def _handle_server_request(self, msg):
        self.on_request(msg)
        return _DEFERRED

    def _write_message(self, payload):
        if payload.get("result") is _DEFERRED:
            return
        super()._write_message(payload)

    def _coerce_notification(self, method, params):
        # Retain the complete wire payload, including fields unknown to the SDK.
        return Notification(method=method, payload=UnknownNotification(params=params or {}))

    def respond(self, request_id: int | str, result: dict) -> None:
        super()._write_message({"id": request_id, "result": result})

    def _request_raw(self, method, params=None):
        return self.request_ordered(method, params)

    def request_ordered(self, method, params=None, on_response=None):
        request_id = str(uuid4())
        waiter = self._router.create_response_waiter(request_id)
        if on_response:
            with self._router._lock:
                self._router.response_callbacks[request_id] = on_response
        try:
            message = {"id": request_id, "method": method}
            if params is not None:
                message["params"] = params
            self._write_message(message)
            try:
                result = waiter.get(timeout=RPC_TIMEOUT)
            except Empty as exc:
                error = TimeoutError(f"Codex RPC {method} 超时")
                if self.on_timeout:
                    self.on_timeout(str(error))
                raise error from exc
            if isinstance(result, BaseException):
                raise result
            return result
        finally:
            self._router.discard_response_waiter(request_id)
            with self._router._lock:
                self._router.response_callbacks.pop(request_id, None)


def command_for(
    machine: dict, arguments: list[str], forward_url: str | None = None
) -> tuple[str, ...]:
    codex = machine.get("codexBin") or "codex"
    command = [codex, *arguments]
    if machine.get("transport") != "ssh":
        return tuple(command)
    target = ssh_target(machine)
    remote_command = f'exec "$SHELL" -lc {shlex.quote(shlex.join(["exec", *command]))}'
    # An independent SSH process keeps the reverse listener tied to this connection.
    forwarding = (
        (
            "-o",
            "ExitOnForwardFailure=yes",
            "-S",
            "none",
            "-R",
            f"127.0.0.1:{REVERSE_PORT}:{urlsplit(forward_url).netloc}",
        )
        if forward_url
        else ()
    )
    return (
        "ssh",
        "-T",
        *SSH_OPTIONS,
        *forwarding,
        "-p",
        str(machine.get("port") or 22),
        "--",
        target,
        remote_command,
    )


class RuntimeConnection:
    def __init__(
        self,
        machine: dict,
        on_notification: Callable[[str, dict], None],
        on_request: Callable[[dict], None],
        on_disconnect: Callable[[str], None],
        nexus_url: str = "http://127.0.0.1:8765",
    ):
        self.machine = machine
        self.nexus_url = nexus_url
        self.on_notification = on_notification
        self.on_request = on_request
        self.on_disconnect = on_disconnect
        self.client: InteractiveCodexClient | None = None
        self.closed = False
        self.version: str | None = None
        self.hidden_threads: set[str] = set()
        self.temporary_events: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = {}

    async def connect(self) -> dict:
        if self.machine.get("connection", "daemon") == "daemon":
            process = await asyncio.create_subprocess_exec(
                *command_for(self.machine, ["app-server", "daemon", "start"]),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **self.machine.get("env", {})},
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
            except (TimeoutError, asyncio.CancelledError) as exc:
                process.kill()
                await process.wait()
                if isinstance(exc, TimeoutError):
                    raise NexusError("启动 Codex 后台服务超时", status_code=504) from exc
                raise
            if process.returncode:
                raise NexusError(
                    (stderr or stdout).decode(errors="replace").strip(), status_code=502
                )
            arguments = ["app-server", "proxy"]
        else:
            arguments = ["app-server", "--listen", "stdio://"]

        command = command_for(self.machine, arguments, forward_url=self.nexus_url)
        if self.machine.get("connection", "daemon") == "daemon":
            command = (sys.executable, "-m", "nexus.ws_bridge", *command)
        config = CodexConfig(
            launch_args_override=command,
            env=self.machine.get("env"),
            client_name="nexus",
            client_title="Nexus",
            client_version="0.1.0",
            experimental_api=True,
        )
        self.client = InteractiveCodexClient(
            config, self.on_request, self._disconnected, self._route_notification
        )
        try:
            self.client.start()
            result = await asyncio.to_thread(self.client.initialize)
            if self.machine.get("transport") == "ssh":
                await self._configure_mcp()
        except Exception as exc:
            raise self.error(exc) from exc
        self.version = self.client._runtime_version
        return result.model_dump(by_alias=True, exclude_none=True)

    async def _configure_mcp(self) -> None:
        await self.rpc(
            "config/batchWrite",
            {
                "edits": [
                    {
                        "keyPath": "mcp_servers.nexus",
                        "mergeStrategy": "replace",
                        "value": {"url": f"http://127.0.0.1:{REVERSE_PORT}/mcp", "enabled": True},
                    }
                ],
                "reloadUserConfig": True,
            },
        )
        await self.rpc("config/mcpServer/reload")

    def _route_notification(self, method: str, params: dict) -> None:
        thread = params.get("thread", {})
        thread_id = params.get("threadId") or thread.get("id")
        if method == "thread/started" and thread.get("ephemeral"):
            self.hidden_threads.add(thread_id)
        if target := self.temporary_events.get(thread_id):
            loop, queue = target
            loop.call_soon_threadsafe(queue.put_nowait, (method, params))
        elif thread_id not in self.hidden_threads:
            self.on_notification(method, params)

    def _disconnected(self, error: str) -> None:
        if not self.closed:
            self.on_disconnect(error)

    async def generate_title(self, thread: dict, text: str) -> str:
        temporary_id = turn_id = None
        start_request = turn_request = None
        completed = False
        try:
            async with asyncio.timeout(30):
                config, catalog = await asyncio.gather(
                    self.rpc("config/read", {"cwd": thread["cwd"]}),
                    self.rpc("model/list", {}),
                )
                model = thread.get("model") or config["config"]["model"]
                if thread["modelProvider"] == "openai" and any(
                    item.get("model", item["id"]) == TITLE_MODEL for item in catalog["data"]
                ):
                    model = TITLE_MODEL
                start_request = asyncio.create_task(
                    self.rpc(
                        "thread/start",
                        {
                            "model": model,
                            "modelProvider": thread["modelProvider"],
                            "cwd": thread["cwd"],
                            "ephemeral": True,
                            "threadSource": "thread_title",
                            "approvalPolicy": "never",
                            "permissions": ":read-only",
                            "runtimeWorkspaceRoots": [],
                            "environments": [],
                            "dynamicTools": [],
                            "selectedCapabilityRoots": [],
                            "config": title_config(config["config"].get("mcp_servers", {})),
                        },
                    )
                )
                started = await asyncio.shield(start_request)
                temporary_id = started["thread"]["id"]
                self.hidden_threads.add(temporary_id)
                queue = asyncio.Queue()
                self.temporary_events[temporary_id] = (asyncio.get_running_loop(), queue)
                turn_request = asyncio.create_task(
                    self.rpc(
                        "turn/start",
                        {
                            "threadId": temporary_id,
                            "input": [{"type": "text", "text": title_prompt(text)}],
                            "outputSchema": TITLE_SCHEMA,
                            **({"effort": "low"} if model == TITLE_MODEL else {}),
                        },
                    )
                )
                result = await asyncio.shield(turn_request)
                turn_id = result["turn"]["id"]
                response = ""
                while True:
                    method, params = await queue.get()
                    if method == "item/completed" and params.get("turnId") == turn_id:
                        if params["item"]["type"] == "agentMessage":
                            response = params["item"]["text"]
                    elif method == "turn/completed" and params["turn"]["id"] == turn_id:
                        completed = True
                        if params["turn"]["status"] != "completed":
                            raise NexusError("Codex title generation failed")
                        return parse_title(response)
        finally:
            # A canceled SDK request still reaches Codex. Collect its ID before cleanup.
            if start_request:
                with suppress(NexusError):
                    temporary_id = (await start_request)["thread"]["id"]
            if turn_request:
                with suppress(NexusError):
                    turn_id = (await turn_request)["turn"]["id"]
            if temporary_id:
                if turn_id and not completed:
                    with suppress(NexusError, TimeoutError):
                        await asyncio.wait_for(
                            self.rpc(
                                "turn/interrupt",
                                {
                                    "threadId": temporary_id,
                                    "turnId": turn_id,
                                },
                            ),
                            5,
                        )
                with suppress(NexusError, TimeoutError):
                    await asyncio.wait_for(
                        self.rpc("thread/unsubscribe", {"threadId": temporary_id}), 5
                    )
                self.temporary_events.pop(temporary_id, None)

    async def rpc(self, method: str, params: dict | None = None, *, on_response=None) -> dict:
        if method == "thread/start":
            params = {"experimentalRawEvents": True, **(params or {})}
        try:
            if on_response:
                return await asyncio.to_thread(
                    self.client.request_ordered, method, params or {}, on_response
                )
            result = await asyncio.to_thread(
                self.client.request, method, params or {}, response_model=RpcResult
            )
            return result.root
        except Exception as exc:
            if isinstance(exc, TransportClosedError):
                self.on_disconnect(str(exc))
            raise self.error(exc) from exc

    async def respond(self, request_id: int | str, result: dict) -> None:
        try:
            await asyncio.to_thread(self.client.respond, request_id, result)
        except Exception as exc:
            raise self.error(exc) from exc

    async def close(self) -> None:
        self.closed = True
        if self.client:
            client, self.client = self.client, None
            process = client._proc
            client._router.fail_all(TransportClosedError("连接已关闭"))
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT):
                    await asyncio.to_thread(client.close)
            except (TimeoutError, OSError) as exc:
                if process and process.poll() is None:
                    process.kill()
                logger.warning(
                    "Could not close Codex connection on %s: %r", self.machine["id"], exc
                )

    @staticmethod
    def error(exc: Exception) -> NexusError:
        if isinstance(exc, NexusError):
            return exc
        if isinstance(exc, JsonRpcError):
            return NexusError(exc.message, exc.code, exc.data)
        if isinstance(exc, TimeoutError):
            return NexusError("连接 Codex 超时", status_code=504)
        if isinstance(exc, (CodexError, OSError)):
            return NexusError(str(exc), status_code=502)
        return NexusError(str(exc), status_code=500)
