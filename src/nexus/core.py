"""The shared session control service used by every Nexus client."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections import defaultdict
from contextlib import aclosing
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from .accounts import AccountLimits
from .commands import CommandExecutor
from .connections import MachineConnections
from .context_records import ContextRecords
from .database import Database
from .errors import NexusError
from .events import EventBroker, now
from .files import FileTransfers
from .history_watch import HistoryWatch
from .message_history import MessageHistory
from .messaging import AgentMessaging, bot_id
from .push import PushNotifications
from .records import MessageRecords
from .runtime import RuntimeConnection
from .search import SearchIndex, SearchSync
from .session_state import SessionState, is_subagent, thread_time
from .shared_context import frozen_usage
from .sides import SideConversations, is_side
from .store import Store
from .terminals import Terminals
from .tool_context import ToolContext

logger = logging.getLogger(__name__)
LIST_TIMEOUT = 15


def _connection_settings(machine: dict) -> dict:
    settings = {
        "transport": "ssh" if machine.get("transport") == "ssh" else "local",
        "connection": "daemon" if machine.get("connection", "daemon") == "daemon" else "spawn",
        "codexBin": machine.get("codexBin") or "codex",
        "env": machine.get("env") or {},
    }
    if settings["transport"] == "ssh":
        settings.update(
            host=machine.get("host"),
            user=machine.get("user") or "",
            port=str(machine.get("port") or 22),
        )
    return settings


def _unmaterialized_history(error: NexusError) -> bool:
    return (
        error.code == -32600
        and any(text in error.message for text in ("no rollout found", "not materialized yet"))
    ) or (error.code == -32601 and error.message == "list_turns is not supported yet")


class SessionService:
    def __init__(
        self,
        data_dir: Path,
        demo: bool = False,
        nexus_url: str = "http://127.0.0.1:8765",
        demo_config: Path | None = None,
    ):
        self.demo = demo or demo_config is not None
        self.demo_config = demo_config
        self.nexus_url = nexus_url
        self.store = Store(Path(data_dir))
        self.database = Database(self.store.directory)
        self.sides = SideConversations(self)
        self.records = MessageRecords(self.database, self.store)
        self.context_records = ContextRecords(self.database)
        self.search_index = SearchIndex(self.database)
        self.search_sync = SearchSync(self, self.search_index)
        self.push = PushNotifications(self.store)
        self.events = EventBroker()
        self.sessions = SessionState()
        self.tool_context = ToolContext(self)
        self.accounts = AccountLimits(self)
        self.runtime = MachineConnections(self)
        self.history_watch = HistoryWatch(self)
        self.files = FileTransfers(self.machine, self.rpc)
        self.commands = CommandExecutor(self.machine)
        self.terminals = Terminals(self.machine, self.files, self.events)
        self.message_history = MessageHistory(self)
        self.messaging = AgentMessaging(self)
        self.listed_threads: dict[str, dict] = {}
        self.observer_tasks: set[asyncio.Task] = set()
        self.project_tasks: dict[str, asyncio.Task] = {}
        self.title_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.pending_requests: dict[str, dict] = {}
        self.native_request_ids: dict[str, Any] = {}
        self.turn_locks = defaultdict(asyncio.Lock)
        self.fork_locks = defaultdict(asyncio.Lock)
        self.reply_locks = defaultdict(asyncio.Lock)
        self.incoming = asyncio.Queue()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.worker: asyncio.Task | None = None
        self.startup_task: asyncio.Task | None = None
        self.demo_network = None
        self.closing = False

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.worker = asyncio.create_task(self._dispatch(), name="nexus-events")
        if self.demo:
            from .demo import DemoNetwork

            self.demo_network = DemoNetwork(
                self._notification, self._server_request, self.demo_config
            )
            for machine in self.demo_network.machines:
                self._start_project_index(machine["id"])
                self.search_sync.wake(machine["id"])
        else:
            self.startup_task = asyncio.create_task(
                self._connect_registered(), name="nexus-connect"
            )

    async def _connect_registered(self) -> None:
        machines = list(self.store.machines)
        results = await asyncio.gather(
            *(self.connect_machine(machine["id"]) for machine in machines),
            return_exceptions=True,
        )
        for machine, result in zip(machines, results):
            if isinstance(result, Exception):
                logger.warning("Failed to connect %s: %s", machine["name"], result)

    async def close(self) -> None:
        self.closing = True
        await self.terminals.close()
        await self.history_watch.close()
        await self.search_sync.close()
        background = [*self.observer_tasks, *self.project_tasks.values()]
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        if self.startup_task:
            self.startup_task.cancel()
            await asyncio.gather(self.startup_task, return_exceptions=True)
        tasks = list(self.title_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.demo_network:
            await self.demo_network.close()
        await self.runtime.close()
        await self.tool_context.close()
        self.incoming.put_nowait(None)
        if self.worker:
            await self.worker
        await self.push.close()
        await self.events.close()
        await asyncio.to_thread(self.database.close)

    def _enqueue(self, kind: str, machine_id: str, source: RuntimeConnection, payload: Any) -> None:
        if not self.closing:
            self.loop.call_soon_threadsafe(
                self.incoming.put_nowait, (kind, machine_id, source, payload)
            )

    async def _dispatch(self) -> None:
        while entry := await self.incoming.get():
            kind, machine_id, source, payload = entry
            if self.runtime.connections.get(machine_id) is not source:
                if kind == "result" and not payload[-1].done():
                    payload[-1].set_exception(NexusError("机器连接已更换", status_code=503))
                continue
            try:
                if kind == "notification":
                    await self._notification(machine_id, payload[0], payload[1])
                elif kind == "request":
                    await self._server_request(machine_id, payload)
                elif kind == "disconnected":
                    await self.runtime.disconnected(machine_id, source, payload)
                elif kind == "result":
                    method, params, result, snapshot, future = payload
                    self.sessions.apply_result(machine_id, method, params, result)
                    if snapshot:
                        result = await self._snapshot_result(machine_id, params["threadId"], result)
                    if not future.done():
                        future.set_result(result)
            except Exception as exc:
                logger.exception("Could not handle %s from %s", kind, machine_id)
                if kind == "result" and not payload[-1].done():
                    payload[-1].set_exception(exc)
                await self.events.publish(machine_id, "nexus/resync", {})

    async def _rpc_connection(
        self,
        machine_id: str,
        connection: RuntimeConnection,
        method: str,
        params: dict,
        *,
        snapshot: bool = False,
    ) -> dict:
        params, tool_token = await self.tool_context.configure(machine_id, method, params)
        future = asyncio.get_running_loop().create_future()
        try:
            await connection.rpc(
                method,
                params,
                on_response=lambda result: self._enqueue(
                    "result",
                    machine_id,
                    connection,
                    (method, params, result, snapshot, future),
                ),
            )
            result = await future
            if tool_token:
                self.tool_context.bind(tool_token, result["thread"]["id"])
            return result
        except BaseException:
            if tool_token:
                self.tool_context.discard(tool_token)
            raise
        finally:
            future.cancel()

    def health(self) -> dict:
        running = bool(self.worker and not self.worker.done() and not self.closing)
        return {
            "status": "ok" if running else "error",
            "eventWorker": "running" if running else "stopped",
        }

    async def _notification(self, machine_id: str, method: str, params: dict) -> None:
        if method in {"account/updated", "account/login/completed", "account/rateLimits/updated"}:
            self.accounts.invalidate(machine_id, identity=method != "account/rateLimits/updated")
        if method == "fs/changed" and self.history_watch.changed(machine_id, params):
            return
        thread_id = params.get("threadId") or params.get("thread", {}).get("id")
        key = (machine_id, thread_id)
        self.tool_context.native_event(machine_id, method, params)
        if method == "rawResponseItem/completed":
            item = self.tool_context.context_item(machine_id, thread_id, params["item"])
            await asyncio.to_thread(self.context_records.record_item, machine_id, item)
            return
        if method == "rawResponse/completed":
            model = self.sessions.thread_settings.get(key, {}).get("model", "")
            await asyncio.to_thread(self.context_records.record_response, machine_id, model, params)
            return
        if key in self.message_history.rewriting and method in {
            "thread/reverted",
            "thread/archived",
            "thread/unarchived",
            "thread/closed",
            "thread/started",
            "thread/status/changed",
            "thread/settings/updated",
        }:
            self.sessions.apply(machine_id, method, params)
            return
        if (
            method in {"item/started", "item/completed"}
            and params["item"]["type"] == "agentMessage"
            and self.sessions.thread_settings.get(key, {}).get("summary") == "none"
        ):
            item_id = params["item"]["id"]
            message_key = (machine_id, thread_id, item_id)
            if method == "item/started" and params.get("startedAtMs") is not None:
                self.sessions.message_starts[message_key] = params["startedAtMs"]
            elif method == "item/completed":
                started = self.sessions.message_starts.pop(message_key, None)
                completed = params.get("completedAtMs")
                tokens = params.get("contentTokenCount")
                if (
                    started is not None
                    and completed is not None
                    and completed > started
                    and tokens is not None
                ):
                    metric = await asyncio.to_thread(
                        self.records.record_throughput,
                        machine_id,
                        thread_id,
                        item_id,
                        tokens,
                        completed - started,
                    )
                else:
                    metric = (
                        await asyncio.to_thread(self.records.read, machine_id, thread_id, item_id)
                    )["nexusTps"]
                params = {**params, "item": {**params["item"], "nexusTps": metric}}
        if method in {
            "turn/completed",
            "thread/closed",
            "thread/archived",
            "thread/deleted",
            "thread/reverted",
        }:
            self.sessions.message_starts = {
                message: start
                for message, start in self.sessions.message_starts.items()
                if message[:2] != key
            }
        self.sessions.apply(machine_id, method, params)
        if (
            method == "thread/settings/updated"
            and params["threadSettings"].get("summary") != "none"
        ):
            self.sessions.message_starts = {
                message: start
                for message, start in self.sessions.message_starts.items()
                if message[:2] != key
            }
        if (
            (method == "thread/started" and not params["thread"].get("ephemeral"))
            or method == "thread/status/changed"
            and params["status"].get("type") == "active"
        ):
            self._start_observer(machine_id, thread_id)
        if method == "turn/completed":
            thread = self.sessions.thread_details.get(key, {})
            if thread.get("cwd") in self.machine(machine_id).get("projects", []) and (
                not isinstance(thread.get("source"), dict) or "subAgent" not in thread["source"]
            ):
                self.push.complete(
                    machine_id,
                    self.machine(machine_id)["name"],
                    thread_id,
                    thread.get("name", ""),
                    params["turn"],
                )
        item_id = (
            params.get("item", {}).get("clientId")
            or params.get("itemId")
            or params.get("item", {}).get("id")
        )
        origin = None
        if method in {"item/started", "item/completed"} and params["item"]["type"] == "userMessage":
            origin = (await asyncio.to_thread(self.records.read, machine_id, thread_id, item_id))[
                "origin"
            ]
        if method == "thread/deleted":
            await self._forget_thread(machine_id, thread_id)
        history_changed = method in {
            "thread/started",
            "thread/name/updated",
            "thread/archived",
            "thread/unarchived",
            "thread/reverted",
            "thread/status/changed",
            "turn/started",
            "turn/completed",
        } or (
            method == "item/completed" and params["item"]["type"] in {"userMessage", "agentMessage"}
        )
        if history_changed and not is_side(self.sessions.thread_details.get(key, {})):
            self.search_sync.wake(
                machine_id,
                thread_id,
                rebuild=method == "thread/reverted",
                content=method in {"turn/started", "turn/completed", "item/completed"},
            )
        await self.events.publish(
            machine_id, method, params, **({"origin": origin} if origin else {})
        )
        if method == "serverRequest/resolved":
            for key, request in list(self.pending_requests.items()):
                if request["machineId"] == machine_id and self.native_request_ids[
                    key
                ] == params.get("requestId"):
                    await self._resolve_request(key)

    async def _server_request(self, machine_id: str, message: dict) -> None:
        key = str(uuid4())
        params = message.get("params") or {}
        if (
            message["method"] == "item/tool/call"
            and params.get("tool") == "read_original"
            and self.tool_context.thread(machine_id, params.get("threadId"))
        ):
            try:
                result = await self.tool_context.read_original(machine_id, params)
            except Exception as error:
                logger.warning("Original tool result read failed: %s", error)
                result = {
                    "contentItems": [
                        {
                            "type": "inputText",
                            "text": "The requested original tool result is unavailable.",
                        }
                    ],
                    "success": False,
                }
            await self.runtime.connections[machine_id].respond(message["id"], result)
            return
        request = {
            "id": key,
            "machineId": machine_id,
            "method": message["method"],
            "params": params,
            "createdAt": now(),
            "threadId": params.get("threadId"),
            "turnId": params.get("turnId"),
        }
        self.pending_requests[key] = request
        self.native_request_ids[key] = message["id"]
        await self.events.publish(
            machine_id,
            "nexus/request/created",
            {
                "request": request,
                "threadId": request["threadId"],
            },
        )

    async def _resolve_request(self, key: str) -> None:
        request = self.pending_requests.pop(key)
        self.native_request_ids.pop(key)
        await self.events.publish(
            request["machineId"],
            "nexus/request/resolved",
            {
                "id": key,
                "threadId": request["threadId"],
            },
        )

    async def _clear_requests(self, machine_id: str) -> None:
        tasks = [task for key, task in self.title_tasks.items() if key[0] == machine_id]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for key, request in list(self.pending_requests.items()):
            if request["machineId"] == machine_id:
                await self._resolve_request(key)
        self.sessions.clear_machine(machine_id)

    def _configurations(self) -> list[dict]:
        return self.demo_network.machines if self.demo_network else self.store.machines

    def machine(self, machine_id: str) -> dict:
        for machine in self._configurations():
            if machine["id"] == machine_id:
                return machine
        raise NexusError("找不到这台机器", status_code=404)

    def _machine_view(self, machine: dict) -> dict:
        default = "connected" if self.demo else "disconnected"
        return {**machine, **self.runtime.status.get(machine["id"], {"status": default})}

    async def _machine_event(self, machine_id: str) -> None:
        self.accounts.invalidate()
        await self.events.publish(
            machine_id,
            "nexus/machine/updated",
            {
                "machine": self._machine_view(self.machine(machine_id)),
            },
        )

    async def machines(self) -> list[dict]:
        return [self._machine_view(machine) for machine in self._configurations()]

    async def add_machine(self, body: dict) -> dict:
        machine = {
            "id": str(uuid4()),
            "name": body.get("name") or body.get("host") or "本机",
            "transport": body.get("transport", "ssh"),
            "host": body.get("host", ""),
            "user": body.get("user", ""),
            "port": body.get("port", 22),
            "codexBin": body.get("codexBin") or "codex",
            "projects": body.get("projects", []),
            "connection": body.get("connection", "daemon"),
        }
        await self._validate_projects(machine, machine["projects"])
        self._configurations().append(machine)
        if not self.demo:
            self.store.save()
        await self._machine_event(machine["id"])
        return self._machine_view(machine)

    async def update_machine(self, machine_id: str, body: dict) -> dict:
        machine = self.machine(machine_id)
        await self._validate_projects(
            {**machine, **body},
            [cwd for cwd in body.get("projects", []) if cwd not in machine.get("projects", [])],
        )
        if _connection_settings(machine) != _connection_settings({**machine, **body}):
            await self.disconnect_machine(machine_id)
        projects_changed = "projects" in body and body["projects"] != machine.get("projects", [])
        machine.update({key: value for key, value in body.items() if key not in {"id", "cwd"}})
        if projects_changed:
            activity = machine.get("projectActivity", {})
            for cwd in list(activity):
                if cwd not in machine["projects"]:
                    del activity[cwd]
        if not self.demo:
            self.store.save()
        await self._machine_event(machine_id)
        if projects_changed:
            self.search_sync.wake(machine_id)
        return self._machine_view(machine)

    async def _validate_projects(self, machine: dict, projects: list[str]) -> None:
        for cwd in projects:
            if self.demo:
                await self.directories(machine["id"], cwd)
            else:
                await self.files.require_directory(machine, cwd)

    async def add_project(self, machine_id: str, cwd: str, create_directory: bool = False) -> dict:
        machine = self.machine(machine_id)
        cwd = cwd.strip()
        if not cwd:
            raise NexusError("请输入项目目录")
        if cwd not in machine.setdefault("projects", []):
            try:
                await self._validate_projects(machine, [cwd])
            except NexusError as exc:
                if not create_directory or exc.code != "directory_not_found":
                    raise
                if self.demo:
                    self.demo_network.create_directory(machine_id, cwd)
                else:
                    await self.files.create_directory(machine, cwd)
                await self._validate_projects(machine, [cwd])
            machine["projects"].append(cwd)
            if not self.demo:
                self.store.save()
            await self._machine_event(machine_id)
            self.search_sync.wake(machine_id)
        return self._machine_view(machine)

    async def remove_project(self, machine_id: str, cwd: str) -> dict:
        machine = self.machine(machine_id)
        cwd = cwd.strip()
        if not cwd:
            raise NexusError("请输入项目目录")
        if cwd in machine.get("projects", []):
            machine["projects"].remove(cwd)
            machine.get("projectActivity", {}).pop(cwd, None)
            if not self.demo:
                self.store.save()
            await self._machine_event(machine_id)
            self.search_sync.wake(machine_id)
        return self._machine_view(machine)

    async def directories(self, machine_id: str, path: str = "~") -> dict:
        directory = await self.files.resolve_path(machine_id, path)
        try:
            result = await self.rpc(machine_id, "fs/readDirectory", {"path": str(directory)})
        except NexusError as exc:
            if exc.code == -32603 and re.search(r"\(os error 2\)$", exc.message):
                raise NexusError(
                    f"项目目录不存在：{directory}", code="directory_not_found", status_code=404
                ) from exc
            if exc.code == -32603 and re.search(r"\(os error 20\)$", exc.message):
                raise NexusError(f"项目路径不是文件夹：{directory}", code="not_directory") from exc
            raise
        entries = [
            {"name": entry["fileName"], "path": str(directory / entry["fileName"])}
            for entry in result["entries"]
            if entry["isDirectory"]
        ]
        entries.sort(key=lambda entry: entry["name"].casefold())
        return {"path": str(directory), "parent": str(directory.parent), "entries": entries}

    async def _index_projects(self, machine: dict, threads: list[dict]) -> None:
        projects = machine.setdefault("projects", [])
        activity = machine.setdefault("projectActivity", {})
        changed = False
        for thread in threads:
            cwd = thread.get("cwd")
            if cwd not in projects or thread.get("ephemeral"):
                continue
            updated = thread_time(thread)
            if updated > activity.get(cwd, 0):
                activity[cwd] = updated
                changed = True
        if changed:
            if not self.demo:
                self.store.save()
            await self._machine_event(machine["id"])

    def _start_project_index(self, machine_id: str) -> None:
        if self.machine(machine_id).get("projectsInitialized") or machine_id in self.project_tasks:
            return
        task = asyncio.create_task(
            self._discover_projects(machine_id), name=f"nexus-projects-{machine_id}"
        )
        self.project_tasks[machine_id] = task
        task.add_done_callback(lambda done: self.project_tasks.pop(machine_id, None))

    async def _discover_projects(self, machine_id: str) -> None:
        try:
            machine = self.machine(machine_id)
            loaded = await self.rpc(machine_id, "thread/loaded/list", {})
            summaries = await asyncio.gather(
                *(self.read_thread(machine_id, thread_id) for thread_id in loaded["data"]),
                return_exceptions=True,
            )
            projects = machine.setdefault("projects", [])
            activity = machine.setdefault("projectActivity", {})
            for result in summaries:
                if isinstance(result, NexusError) and result.code == "empty_thread":
                    continue
                if isinstance(result, Exception):
                    raise result
                thread = result["thread"]
                if thread.get("status", {}).get("type") != "active" or thread.get("ephemeral"):
                    continue
                cwd = thread.get("cwd")
                if not cwd:
                    continue
                if cwd not in projects:
                    projects.append(cwd)
                activity[cwd] = max(activity.get(cwd, 0), thread_time(thread))
            machine["projectsInitialized"] = True
            if not self.demo:
                self.store.save()
            await self._machine_event(machine_id)
            self.search_sync.wake(machine_id)
        except Exception as exc:
            logger.warning("Could not index projects on %s: %s", machine_id, exc)

    async def remove_machine(self, machine_id: str) -> None:
        machine = self.machine(machine_id)
        await self.terminals.close(machine_id)
        await self.disconnect_machine(machine_id)
        self._configurations().remove(machine)
        self.sessions.forget_machine(machine_id)
        self.listed_threads.pop(machine_id, None)
        self.runtime.status.pop(machine_id, None)
        await asyncio.to_thread(self.search_index.remove, machine_id)
        if not self.demo:
            self.store.save()
        await self.events.publish(machine_id, "nexus/machine/removed", {"machineId": machine_id})

    async def connect_machine(self, machine_id: str) -> dict:
        return await self.runtime.connect(machine_id)

    async def reconnect_machine(self, machine_id: str) -> dict:
        return await self.runtime.reconnect(machine_id)

    async def _observe_thread(
        self, machine_id: str, thread_id: str, connection: RuntimeConnection
    ) -> None:
        key = (machine_id, thread_id)
        await self.message_history.ready(machine_id, thread_id)
        if (
            key in self.sessions.observed_threads
            or self.runtime.connections.get(machine_id) is not connection
        ):
            return
        try:
            summary = await self._rpc_connection(
                machine_id, connection, "thread/read", {"threadId": thread_id}
            )
            thread = summary["thread"]
            if (
                thread.get("cwd") not in self.machine(machine_id).get("projects", [])
                or thread.get("ephemeral")
                or isinstance(thread.get("source"), dict)
                and "subAgent" in thread["source"]
            ):
                return
            if not await self._can_list_thread(machine_id, summary["thread"], connection):
                return
            await self._load_reasoning_summary(machine_id, thread, connection)
            result = await self._rpc_connection(
                machine_id,
                connection,
                "thread/resume",
                {"threadId": thread_id, "excludeTurns": True},
            )
            if self.runtime.connections.get(machine_id) is not connection:
                return
            self.sessions.observed_threads.add(key)
            self.sessions.resume_threads.add(key)
            self.sessions.remember_thread(machine_id, result["thread"])
            self.sessions.remember_settings(key, result)
            if (
                key in self.sessions.recovering_turns
                or result["thread"].get("status", {}).get("type") == "active"
            ):
                latest = await self._rpc_connection(
                    machine_id,
                    connection,
                    "thread/turns/list",
                    {
                        "threadId": thread_id,
                        "limit": 1,
                        "itemsView": "notLoaded",
                        "sortDirection": "desc",
                    },
                )
                self.sessions.remember_turns(machine_id, thread_id, latest["data"])
                for turn in latest["data"]:
                    if (
                        turn["id"] == self.sessions.recovering_turns.get(key)
                        and turn["status"] != "inProgress"
                    ):
                        await self._notification(
                            machine_id, "turn/completed", {"threadId": thread_id, "turn": turn}
                        )
        except NexusError as exc:
            logger.warning("Could not restore session %s/%s: %s", machine_id, thread_id, exc)

    async def _restore_observers(self, machine_id: str, connection: RuntimeConnection) -> None:
        threads = {
            thread_id for host, thread_id in self.sessions.resume_threads if host == machine_id
        }
        try:
            loaded = await self._rpc_connection(machine_id, connection, "thread/loaded/list", {})
            threads.update(loaded["data"])
        except NexusError as exc:
            logger.warning("Could not discover sessions on %s: %s", machine_id, exc)
        await asyncio.gather(
            *(
                self._observe_thread(machine_id, thread_id, connection)
                for thread_id in threads
                if thread_id not in connection.hidden_threads
            )
        )

    def _start_observer(self, machine_id: str, thread_id: str | None = None) -> None:
        connection = self.runtime.connections.get(machine_id)
        if connection is None:
            return
        work = (
            self._observe_thread(machine_id, thread_id, connection)
            if thread_id
            else self._restore_observers(machine_id, connection)
        )
        task = asyncio.create_task(work, name="nexus-observe")
        self.observer_tasks.add(task)
        task.add_done_callback(self.observer_tasks.discard)

    def observe_loaded(self) -> None:
        for machine_id in self.runtime.connections:
            self._start_observer(machine_id)

    async def disconnect_machine(self, machine_id: str) -> dict:
        return await self.runtime.disconnect(machine_id)

    async def _forget_thread(self, machine_id: str, thread_id: str) -> None:
        self.sides.forget(machine_id, thread_id)
        self.search_sync.forget(machine_id, thread_id)
        await asyncio.to_thread(self.search_index.remove, machine_id, thread_id)
        await self.events.publish(machine_id, "nexus/search/updated", {})

    async def rpc(self, machine_id: str, method: str, params: dict | None = None) -> dict:
        self.machine(machine_id)
        params = params or {}
        key = (machine_id, params.get("threadId"))
        title_thread = None
        if method == "turn/start" and (fresh := self.sessions.fresh_threads.get(key)):
            if not fresh.get("name") and not fresh["turns"]:
                title_thread = {**deepcopy(fresh), **self.sessions.thread_settings.get(key, {})}
                if params.get("model"):
                    title_thread["model"] = params["model"]
        if method == "thread/name/set":
            task = self.title_tasks.get(key)
            if task and task is not asyncio.current_task():
                task.cancel()
        try:
            if self.demo_network:
                params, tool_token = await self.tool_context.configure(machine_id, method, params)
                result = await self.demo_network.rpc(machine_id, method, params)
                if tool_token:
                    self.tool_context.bind(tool_token, result["thread"]["id"])
                self.sessions.apply_result(machine_id, method, params, result)
            else:
                result = await self._rpc_connection(
                    machine_id, self.runtime.connection(machine_id), method, params
                )
        except NexusError as exc:
            # New threads exist in memory before Codex materializes a rollout.
            if key in self.sessions.fresh_threads and _unmaterialized_history(exc):
                if method == "thread/read":
                    return {"thread": deepcopy(self.sessions.fresh_threads[key])}
                if method == "thread/turns/list":
                    turns = deepcopy(self.sessions.fresh_threads[key]["turns"])
                    if params.get("sortDirection", "desc") == "desc":
                        turns.reverse()
                    return {"data": turns[: params.get("limit", 30)], "nextCursor": None}
            raise
        if method == "turn/start" and result.get("turn") and title_thread and not self.demo:
            text = "\n".join(item["text"] for item in params["input"] if item["type"] == "text")
            if text.strip():
                task = asyncio.create_task(self._generate_title(key, title_thread, text))
                self.title_tasks[key] = task
                task.add_done_callback(lambda done: self.title_tasks.pop(key, None))
        if method == "thread/delete":
            await self._forget_thread(machine_id, params["threadId"])
        if method in {
            "thread/start",
            "thread/fork",
            "thread/name/set",
            "thread/archive",
            "thread/unarchive",
            "thread/revert",
            "thread/settings/update",
            "turn/start",
            "turn/steer",
        }:
            changed_id = result.get("thread", {}).get("id") or params.get("threadId")
            if not is_side(self.sessions.thread_details.get((machine_id, changed_id), {})):
                self.search_sync.wake(machine_id, changed_id, rebuild=method == "thread/revert")
        if method in {"thread/start", "thread/fork"}:
            await self._load_reasoning_summary(machine_id, result["thread"])
        return result

    async def search_threads(
        self,
        term: str,
        mode: str = "title",
        view: str = "recent",
        *,
        machine_id: str | None = None,
        cwd: str | None = None,
        archived: bool | None = None,
        cursor: str | None = None,
        limit: int = 10,
    ) -> dict:
        machines = [self.machine(machine_id)] if machine_id else self._configurations()
        projects = [
            (machine["id"], project)
            for machine in machines
            for project in machine.get("projects", [])
            if mode == "id" or not cwd or project == cwd
        ]
        runtime = {
            (host, thread["id"]): thread
            for host, page in self.listed_threads.items()
            for thread in page["data"]
        }
        for key, thread in self.sessions.thread_details.items():
            runtime[key] = {**runtime.get(key, {}), **thread}
        waiting = {
            (request["machineId"], request["threadId"])
            for request in self.pending_requests.values()
            if request.get("threadId")
        }
        waiting.update(
            key
            for key, thread in runtime.items()
            if set(thread.get("status", {}).get("activeFlags", []))
            & {"waitingOnApproval", "waitingOnUserInput"}
        )
        allowed = None
        if view == "waiting":
            allowed = waiting
        elif view == "working":
            allowed = (
                set(self.sessions.active_turns)
                | {
                    key
                    for key, thread in runtime.items()
                    if thread.get("status", {}).get("type") == "active"
                }
            ) - waiting
        if allowed is not None:
            allowed = {
                key
                for key in allowed
                if self.runtime.status.get(key[0], {}).get("status") != "disconnected"
            }
        result = await asyncio.to_thread(
            self.search_index.query,
            term.strip(),
            mode,
            projects,
            allowed=allowed,
            archived=archived,
            cursor=cursor,
            limit=limit,
        )
        names = {machine["id"]: machine["name"] for machine in machines}
        for thread in result["data"]:
            thread["botId"] = bot_id(thread["id"])
            thread["machineName"] = names[thread["machineId"]]
            key = (thread["machineId"], thread["id"])
            if key in runtime and "status" in runtime[key]:
                thread["status"] = runtime[key]["status"]
        result["syncing"] = any(machine["id"] in self.search_sync.busy for machine in machines)
        result["errors"] = [
            {"machineId": machine["id"], "message": self.search_sync.errors[machine["id"]]}
            for machine in machines
            if machine["id"] in self.search_sync.errors
        ]
        return result

    async def _generate_title(self, key: tuple[str, str], thread: dict, text: str) -> None:
        machine_id, thread_id = key
        try:
            name = await self.runtime.connections[machine_id].generate_title(thread, text)
            current = await self.rpc(machine_id, "thread/read", {"threadId": thread_id})
            if not current["thread"].get("name"):
                await self.rpc(machine_id, "thread/name/set", {"threadId": thread_id, "name": name})
        except (NexusError, TimeoutError, ValueError) as exc:
            logger.warning("Could not generate title for %s/%s: %s", machine_id, thread_id, exc)

    async def _can_list_thread(
        self, machine_id: str, thread: dict, connection: RuntimeConnection | None = None
    ) -> bool:
        if fresh := self.sessions.fresh_threads.get((machine_id, thread["id"])):
            thread["archived"] = fresh["archived"]
            if not fresh["turns"]:
                for field in ("createdAt", "updatedAt", "recencyAt"):
                    if field in fresh:
                        thread[field] = fresh[field]
            return True
        if thread.get("preview"):
            return True
        # Empty editor entries can outlive their rollout and synthesize a new
        # timestamp on every read. Require readable turns before indexing them.
        try:
            rpc = (
                connection.rpc
                if connection
                else lambda method, params: self.rpc(machine_id, method, params)
            )
            result = await rpc(
                "thread/turns/list",
                {
                    "threadId": thread["id"],
                    "limit": 1,
                    "itemsView": "notLoaded",
                },
            )
        except NexusError as exc:
            if _unmaterialized_history(exc):
                return False
            raise
        return bool(result["data"])

    async def read_thread(self, machine_id: str, thread_id: str) -> dict:
        try:
            result = await self.rpc(machine_id, "thread/read", {"threadId": thread_id})
            result["thread"]["botId"] = bot_id(result["thread"]["id"])
            if side := self.sides.metadata(machine_id, thread_id):
                result["thread"]["sideConversation"] = side
                return result
            if await self._can_list_thread(machine_id, result["thread"]):
                return result
        except NexusError as exc:
            if not _unmaterialized_history(exc):
                raise
        raise NexusError("会话尚无已保存的对话记录", code="empty_thread", status_code=404)

    async def list_threads(self, machine_id: str | None = None, params: dict | None = None) -> dict:
        params = dict(params or {})
        scope = params.pop("scope", "loaded")
        params = {
            "sourceKinds": ["cli", "vscode", "appServer", "exec"],
            "modelProviders": [],
            "sortKey": "recency_at",
            "limit": 50,
            **params,
        }
        machines = [self.machine(machine_id)] if machine_id else self._configurations()

        async def fetch(machine: dict) -> dict:
            projects = machine.get("projects", [])
            cwd = params.get("cwd")
            directories = projects
            if cwd:
                directories = [cwd] if cwd in projects else []
            if not directories:
                return {"data": [], "nextCursor": None}
            state = self.runtime.status.get(machine["id"], {})
            if state.get("status") == "disconnected":
                return {"data": [], "nextCursor": None}
            if state.get("status") in {"error", "connecting", "reconnecting"}:
                if state.get("status") in {"connecting", "reconnecting"} and scope == "loaded":
                    cached = deepcopy(
                        self.listed_threads.get(machine["id"], {"data": [], "nextCursor": None})
                    )
                    cached["data"] = [
                        thread
                        for thread in cached["data"]
                        if thread.get("cwd") in directories
                        and not is_subagent(thread)
                        and (
                            params.get("archived") is None
                            or bool(thread.get("archived")) == params["archived"]
                        )
                    ]
                    return cached
                raise NexusError(state.get("error") or "机器正在连接", status_code=502)
            if scope == "loaded":
                loaded = await self.rpc(machine["id"], "thread/loaded/list", {})
                hidden = (
                    self.runtime.connections[machine["id"]].hidden_threads
                    if not self.demo
                    else set()
                )
                summaries = await asyncio.gather(
                    *(
                        self.read_thread(machine["id"], thread_id)
                        for thread_id in loaded["data"]
                        if thread_id not in hidden
                    ),
                    return_exceptions=True,
                )
                threads = []
                for summary in summaries:
                    if isinstance(summary, NexusError) and summary.code == "empty_thread":
                        continue
                    if isinstance(summary, Exception):
                        raise summary
                    thread = summary["thread"]
                    if (
                        not thread.get("ephemeral")
                        and not is_side(thread)
                        and not is_subagent(thread)
                        and thread.get("cwd") in directories
                        and (
                            params.get("archived") is None
                            or bool(thread.get("archived")) == params["archived"]
                        )
                    ):
                        threads.append(thread)
                if term := params.get("searchTerm"):
                    term = term.lower()
                    threads = [
                        thread
                        for thread in threads
                        if term
                        in " ".join(
                            thread.get(field) or "" for field in ("name", "preview", "cwd")
                        ).lower()
                    ]
                threads.sort(
                    key=lambda thread: thread_time(thread, params["sortKey"]), reverse=True
                )
                result = {"data": threads, "nextCursor": None}
            elif params.get("archived") is None:
                result = await self._list_all_threads(machine["id"], params)
            else:
                threads = []
                cursor = params.get("cursor")
                limit = params["limit"]
                while True:
                    result = await self.rpc(
                        machine["id"],
                        "thread/list",
                        {
                            **params,
                            "cwd": directories,
                            "cursor": cursor,
                            "limit": limit - len(threads),
                        },
                    )
                    visible = await asyncio.gather(
                        *(self._can_list_thread(machine["id"], thread) for thread in result["data"])
                    )
                    threads.extend(
                        thread
                        for thread, show in zip(result["data"], visible)
                        if show and not is_side(thread) and not is_subagent(thread)
                    )
                    cursor = result.get("nextCursor")
                    if len(threads) >= limit or not cursor:
                        break
                result["data"] = threads
            result["data"] = [
                {
                    **thread,
                    "botId": bot_id(thread["id"]),
                    "machineId": machine["id"],
                    "machineName": machine["name"],
                    "archived": bool(thread.get("archived"))
                    if params.get("archived") is None
                    else bool(params["archived"]),
                }
                for thread in result.get("data", [])
            ]
            await self._index_projects(machine, result["data"])
            if (
                scope == "loaded"
                and params.get("archived") is None
                and not params.get("searchTerm")
            ):
                self.listed_threads[machine["id"]] = deepcopy(result)
            return result

        async def bounded_fetch(machine: dict) -> dict:
            try:
                async with asyncio.timeout(LIST_TIMEOUT):
                    return await fetch(machine)
            except TimeoutError as exc:
                raise NexusError("读取会话列表超时", status_code=504) from exc

        if machine_id:
            return await bounded_fetch(machines[0])
        results = await asyncio.gather(
            *(bounded_fetch(machine) for machine in machines), return_exceptions=True
        )
        data, errors, cursors = [], [], {}
        for machine, result in zip(machines, results):
            if isinstance(result, Exception):
                errors.append({"machineId": machine["id"], "message": str(result)})
            else:
                data.extend(result["data"])
                if result.get("nextCursor"):
                    cursors[machine["id"]] = result["nextCursor"]
        data.sort(key=lambda thread: thread_time(thread, params["sortKey"]), reverse=True)
        return {"data": data, "errors": errors, "nextCursors": cursors}

    async def _list_all_threads(self, machine_id: str, params: dict) -> dict:
        # Retain each native page's start and consumed offset to resume the two sorted lists.
        page_size = min(params["limit"], 100)
        positions = [{"cursor": None, "offset": 0} for _ in range(2)]
        if cursor := params.get("cursor"):
            try:
                page_size, positions = json.loads(base64.urlsafe_b64decode(cursor))
            except (ValueError, TypeError) as exc:
                raise NexusError("会话分页位置无效") from exc
        pages = [None, None]

        async def load(group: int) -> None:
            while (position := positions[group]) is not None:
                page = await self.list_threads(
                    machine_id,
                    {
                        **params,
                        "scope": "history",
                        "archived": bool(group),
                        "cursor": position["cursor"],
                        "limit": page_size,
                    },
                )
                if position["offset"] < len(page["data"]):
                    pages[group] = page
                    return
                positions[group] = (
                    {"cursor": page["nextCursor"], "offset": 0} if page.get("nextCursor") else None
                )

        await asyncio.gather(load(0), load(1))
        threads = []
        while len(threads) < params["limit"]:
            available = [group for group in range(2) if positions[group] is not None]
            if not available:
                break
            group = max(
                available,
                key=lambda group: thread_time(
                    pages[group]["data"][positions[group]["offset"]], params["sortKey"]
                ),
            )
            position, page = positions[group], pages[group]
            threads.append(page["data"][position["offset"]])
            position["offset"] += 1
            if position["offset"] == len(page["data"]):
                positions[group] = (
                    {"cursor": page["nextCursor"], "offset": 0} if page.get("nextCursor") else None
                )
                if len(threads) < params["limit"]:
                    await load(group)
        next_cursor = None
        if any(position is not None for position in positions):
            next_cursor = base64.urlsafe_b64encode(
                json.dumps([page_size, positions]).encode()
            ).decode()
        return {"data": threads, "nextCursor": next_cursor}

    async def fork_thread(self, machine_id: str, thread_id: str, params: dict) -> dict:
        item_id = params.get("lastItemId")
        if not item_id:
            return await self._fork_thread(machine_id, thread_id, params)
        turn_id = params.get("turnId")
        if not turn_id or params.get("lastTurnId") or params.get("beforeTurnId"):
            raise NexusError("消息分叉需要 turnId 和 lastItemId，并单独使用这一边界")
        async with self.turn_locks[(machine_id, thread_id)]:
            prefix = await self.message_history.prefix(
                machine_id, thread_id, turn_id, item_id, inclusive=True
            )
            native = {
                key: value for key, value in params.items() if key not in {"turnId", "lastItemId"}
            }
            return await self._fork_thread(
                machine_id,
                thread_id,
                {
                    **native,
                    "beforeTurnId": turn_id,
                    "deferGoalContinuation": True,
                },
                prefix,
            )

    async def _fork_name(self, machine_id: str, source: dict) -> str:
        title = (
            source.get("name")
            or source.get("title")
            or (source.get("preview") or "新会话").split("\n")[0]
        )
        match = re.fullmatch(r"(.*?) \((\d+)\)", title)
        base = match[1] if match else title
        number = max(2, int(match[2]) + 1) if match else 2
        pattern = re.compile(re.escape(base) + r" \((\d+)\)")

        async def names(archived: bool) -> list[str]:
            cursor = None
            result = []
            while True:
                page = await self.rpc(
                    machine_id,
                    "thread/list",
                    {
                        "searchTerm": base,
                        "archived": archived,
                        "limit": 100,
                        "sourceKinds": [],
                        "modelProviders": [],
                        "cursor": cursor,
                    },
                )
                result.extend(thread.get("name") or "" for thread in page["data"])
                cursor = page.get("nextCursor")
                if not cursor:
                    return result

        for group in await asyncio.gather(names(False), names(True)):
            for name in group:
                if existing := pattern.fullmatch(name):
                    number = max(number, int(existing[1]) + 1)
        return f"{base} ({number})"

    async def _fork_thread(
        self, machine_id: str, thread_id: str, params: dict, prefix=None
    ) -> dict:
        async with self.fork_locks[machine_id]:
            source = (await self.rpc(machine_id, "thread/read", {"threadId": thread_id}))["thread"]
            side = self.sides.saved.get((machine_id, thread_id))
            if side:
                await self.sides.ensure_context(machine_id, thread_id, source)
                number = self.sides.next_number(machine_id, side["parent_thread_id"])
                name = f"侧聊 {number}"
                params = {**params, "threadSource": "nexus-side"}
            else:
                name = await self._fork_name(machine_id, source)
            if source.get("archived"):
                async with self.message_history.rewrite(machine_id, thread_id):
                    await self.rpc(machine_id, "thread/unarchive", {"threadId": thread_id})
                    try:
                        result = await self.rpc(
                            machine_id, "thread/fork", {**params, "threadId": thread_id}
                        )
                    finally:
                        await self.rpc(machine_id, "thread/archive", {"threadId": thread_id})
            else:
                result = await self.rpc(
                    machine_id, "thread/fork", {**params, "threadId": thread_id}
                )
            fork_id = result["thread"]["id"]
            try:
                await self.rpc(machine_id, "thread/name/set", {"threadId": fork_id, "name": name})
                result["thread"]["name"] = name
                if prefix:
                    settings = deepcopy(
                        self.sessions.thread_settings.get((machine_id, fork_id), {})
                    )
                    async with self.message_history.rewrite(machine_id, fork_id):
                        await self.message_history.append(machine_id, fork_id, prefix, settings)
                    result["thread"] = (await self.read_thread(machine_id, fork_id))["thread"]
                if side:
                    return await self.sides.record(
                        machine_id,
                        result,
                        side["parent_thread_id"],
                        side["inherited_turn_id"],
                        json.loads(side["shared_context"]),
                        number,
                    )
                return result
            except Exception:
                await self.rpc(machine_id, "thread/delete", {"threadId": fork_id})
                raise

    async def history(
        self, machine_id: str, thread_id: str, cursor: str | None = None, limit: int = 30
    ) -> dict:
        await self.message_history.ready(machine_id, thread_id)
        summary = await self.read_thread(machine_id, thread_id)
        # A broadcast thread/started doesn't subscribe us to scoped items.
        # Opening a conversation explicitly attaches this connection once.
        if (
            not summary["thread"].get("archived")
            and (machine_id, thread_id) not in self.sessions.observed_threads
            and (machine_id, thread_id) not in self.sessions.fresh_threads
        ):
            await self.rpc(
                machine_id, "thread/resume", {"threadId": thread_id, "excludeTurns": True}
            )
        params = {
            "threadId": thread_id,
            "limit": limit,
            "itemsView": "full",
            "sortDirection": "desc",
        }
        if cursor:
            params["cursor"] = cursor
        result = await self.rpc(machine_id, "thread/turns/list", params)
        result = self.sides.visible(machine_id, thread_id, result)
        await asyncio.to_thread(
            self.records.annotate, machine_id, thread_id, result.get("data", [])
        )
        self.tool_context.annotate(machine_id, thread_id, result.get("data", []))
        return {"thread": summary["thread"], **result}

    async def _load_reasoning_summary(
        self, machine_id: str, thread: dict, connection: RuntimeConnection | None = None
    ) -> None:
        key = (machine_id, thread["id"])
        if "summary" in self.sessions.thread_settings.get(key, {}):
            return
        params = {"cwd": thread["cwd"]}
        result = (
            await self._rpc_connection(machine_id, connection, "config/read", params)
            if connection
            else await self.rpc(machine_id, "config/read", params)
        )
        self.sessions.thread_settings.setdefault(key, {}).setdefault(
            "summary",
            result["config"].get("model_reasoning_summary") or "auto",
        )

    async def thread_state(self, machine_id: str, thread_id: str) -> dict:
        await self.message_history.ready(machine_id, thread_id)
        key = (machine_id, thread_id)
        result = await self.read_thread(machine_id, thread_id)
        await self._load_reasoning_summary(machine_id, result["thread"])
        await self.sides.ensure_context(machine_id, thread_id, result["thread"])
        if (
            not result["thread"].get("archived")
            and key not in self.sessions.observed_threads
            and key not in self.sessions.fresh_threads
        ):
            result = await self.rpc(
                machine_id, "thread/resume", {"threadId": thread_id, "excludeTurns": True}
            )
        if (
            not result["thread"].get("archived")
            and key not in self.sessions.fresh_threads
            and (
                key not in self.sessions.compactions_loaded
                or key not in self.sessions.token_usage
                and key not in self.sessions.usage_bootstrapped
            )
        ):
            # 0.169 replays saved usage on a warm connection only when turns
            # are included. The same read initializes the compaction total.
            self.sessions.usage_bootstrapped.add(key)
            result = await self.rpc(
                machine_id, "thread/resume", {"threadId": thread_id, "excludeTurns": False}
            )
            await asyncio.sleep(0)
        if result["thread"].get("archived") and key not in self.sessions.compactions_loaded:
            result = await self.rpc(
                machine_id, "thread/read", {"threadId": thread_id, "includeTurns": True}
            )
        if key not in self.sessions.token_usage and self.sides.metadata(machine_id, thread_id):
            usage = await frozen_usage(self, machine_id, result["thread"])
            if usage is not None:
                self.sessions.token_usage.setdefault(key, usage)
        thread = deepcopy(result["thread"])
        thread["turns"] = []
        if thread.get("ephemeral"):
            self.sessions.goal_support[key] = False
        else:
            await self.read_goal(machine_id, thread_id)
        if side := self.sides.metadata(machine_id, thread_id):
            thread["sideConversation"] = side
        return {
            "thread": thread,
            "settings": {
                "model": None,
                "reasoningEffort": None,
                "serviceTier": None,
                **deepcopy(self.sessions.thread_settings.get(key, {})),
                **self.tool_context.settings(machine_id, thread_id),
            },
            "tokenUsage": deepcopy(self.sessions.token_usage.get(key)),
            "goal": deepcopy(self.sessions.goals.get(key)),
            "goalSupported": self.sessions.goal_support.get(key, True),
            "compactionIds": sorted(self.sessions.compaction_ids.get(key, set())),
        }

    async def _snapshot_result(self, machine_id: str, thread_id: str, result: dict) -> dict:
        key = (machine_id, thread_id)
        page = result.get("initialTurnsPage") or result
        snapshot = {
            "thread": deepcopy(self.sessions.thread_details[key]),
            "settings": {
                **deepcopy(self.sessions.thread_settings.get(key, {})),
                **self.tool_context.settings(machine_id, thread_id),
            },
            "tokenUsage": deepcopy(self.sessions.token_usage.get(key)),
            "goal": deepcopy(self.sessions.goals.get(key)),
            "goalSupported": self.sessions.goal_support.get(key, True),
            "compactionIds": sorted(self.sessions.compaction_ids.get(key, set())),
            "data": deepcopy(page.get("data", [])),
            "nextCursor": page.get("nextCursor"),
            "eventCursor": self.events.cursor(),
        }
        if side := self.sides.metadata(machine_id, thread_id):
            snapshot["thread"]["sideConversation"] = side
            snapshot = self.sides.visible(machine_id, thread_id, snapshot)
        await asyncio.to_thread(self.records.annotate, machine_id, thread_id, snapshot["data"])
        self.tool_context.annotate(machine_id, thread_id, snapshot["data"])
        return snapshot

    async def snapshot(self, machine_id: str, thread_id: str, limit: int = 30) -> dict:
        await self.message_history.ready(machine_id, thread_id)
        state = await self.thread_state(machine_id, thread_id)
        fresh = self.sessions.fresh_threads.get((machine_id, thread_id))
        if fresh and not fresh.get("turns"):
            return await self._snapshot_result(machine_id, thread_id, {"data": []})
        if state["thread"].get("archived"):
            method = "thread/turns/list"
            params = {
                "threadId": thread_id,
                "limit": limit,
                "itemsView": "full",
                "sortDirection": "desc",
            }
        else:
            method = "thread/resume"
            params = {
                "threadId": thread_id,
                "excludeTurns": True,
                "initialTurnsPage": {"limit": limit, "itemsView": "full", "sortDirection": "desc"},
            }
        if self.demo:
            result = await self._snapshot_result(
                machine_id, thread_id, await self.rpc(machine_id, method, params)
            )
        else:
            result = await self._rpc_connection(
                machine_id, self.runtime.connection(machine_id), method, params, snapshot=True
            )
        while result["nextCursor"] and len(result["data"]) < limit:
            page = await self.history(
                machine_id, thread_id, result["nextCursor"], limit - len(result["data"])
            )
            known = {turn["id"] for turn in result["data"]}
            result["data"].extend(turn for turn in page["data"] if turn["id"] not in known)
            result["nextCursor"] = page["nextCursor"]
        return result

    def remember_session_defaults(self, settings: dict, fields: list[str]) -> None:
        model = settings.get("model")
        if not model:
            return
        defaults = deepcopy(self.store.session_defaults)
        if "model" in fields:
            defaults["model"] = model
        preferences = defaults["models"].get(model, {}).copy()
        if "effort" in fields and settings.get("reasoningEffort") is not None:
            preferences["effort"] = settings["reasoningEffort"]
        if "serviceTier" in fields:
            tier = settings.get("serviceTier") or "default"
            preferences["serviceTier"] = "priority" if tier == "fast" else tier
        if preferences:
            defaults["models"][model] = preferences
        if defaults != self.store.session_defaults:
            self.store.data["sessionDefaults"] = defaults
            self.store.save()

    def model_preferences(self, preset: dict) -> dict:
        saved = self.store.session_defaults["models"].get(preset["model"], {})
        settings = {}
        effort = saved.get("effort")
        if any(
            option["reasoningEffort"] == effort for option in preset["supportedReasoningEfforts"]
        ):
            settings["effort"] = effort
        tiers = {"default", *(tier["id"] for tier in preset.get("serviceTiers", []))}
        tiers.update(
            "priority" if tier == "fast" else tier
            for tier in preset.get("additionalSpeedTiers", [])
        )
        tier = saved.get("serviceTier")
        if tier in tiers:
            settings["serviceTier"] = tier
        return settings

    async def model_switch_defaults(self, machine_id: str, model: str) -> dict:
        result = await self.rpc(machine_id, "model/list", {})
        preset = next((entry for entry in result["data"] if entry["model"] == model), None)
        if preset is None:
            return {"serviceTier": "default"}
        return {
            "effort": preset["defaultReasoningEffort"],
            "serviceTier": preset.get("defaultServiceTier") or "default",
            **self.model_preferences(preset),
        }

    async def apply_session_defaults(self, machine_id: str, params: dict) -> None:
        defaults = self.store.session_defaults
        if not defaults.get("model") and not defaults["models"]:
            return
        config = params.get("config") or {}
        model = params.get("model") or config.get("model")
        model_provider = params.get("modelProvider") or config.get("model_provider")
        result = await self.rpc(
            machine_id, "model/list", {"modelProvider": model_provider} if model_provider else {}
        )
        catalog = {entry["model"]: entry for entry in result["data"]}
        if not model and defaults.get("model") in catalog:
            model = params["model"] = defaults["model"]
        if not model and defaults["models"]:
            config_params = {"cwd": params["cwd"]} if params.get("cwd") else {}
            effective = (await self.rpc(machine_id, "config/read", config_params))["config"]
            model = effective.get("model") or next(
                (entry["model"] for entry in catalog.values() if entry.get("isDefault")),
                None,
            )
        if model in catalog:
            preferences = self.model_preferences(catalog[model])
            if "effort" in preferences and "model_reasoning_effort" not in config:
                params["config"] = {**config, "model_reasoning_effort": preferences["effort"]}
            if (
                "serviceTier" in preferences
                and "serviceTier" not in params
                and "service_tier" not in config
            ):
                params["serviceTier"] = preferences["serviceTier"]

    async def update_thread_settings(
        self,
        machine_id: str,
        thread_id: str,
        settings: dict,
        save_defaults: list[str] | None = None,
    ) -> dict:
        previous = await self.thread_state(machine_id, thread_id)
        settings = dict(settings)
        if "toolCallId" in settings:
            raise NexusError("Tool call ID 在会话创建时确定", status_code=409)
        if "toolCallCompact" in settings:
            tool_settings = await self.tool_context.set_compact(
                machine_id, thread_id, settings.pop("toolCallCompact")
            )
            previous["settings"].update(tool_settings)
            if not settings:
                return {**previous, "appliesTo": "activeTurn"}
        params = {}
        if (
            save_defaults
            and (model := settings.get("model"))
            and model != previous["settings"]["model"]
        ):
            params = await self.model_switch_defaults(machine_id, model)
        params.update(settings)
        if "reasoningEffort" in params:
            params["effort"] = params.pop("reasoningEffort")
        if "sandbox" in params:
            params["sandboxPolicy"] = params.pop("sandbox")
        await self.rpc(machine_id, "thread/settings/update", {**params, "threadId": thread_id})
        active = await self.active_turn_id(machine_id, thread_id)
        dynamic = {
            field: params[field] for field in ("model", "effort", "serviceTier") if field in params
        }
        applies_to = "nextTurn"
        if active and dynamic:
            try:
                result = await self.rpc(
                    machine_id,
                    "turn/settings/update",
                    {
                        "threadId": thread_id,
                        "turnId": active,
                        **dynamic,
                    },
                )
            except NexusError as exc:
                if (
                    exc.code != -32600
                    or exc.message
                    != "turn settings updates require the step_model_switching feature"
                ):
                    raise
            else:
                if result["status"] == "applied":
                    applies_to = "activeTurn"
        if (machine_id, thread_id) not in self.sessions.fresh_threads:
            # Read the runtime's effective configuration after the mutation;
            # its settings notification can arrive after the RPC response.
            await self.rpc(
                machine_id, "thread/resume", {"threadId": thread_id, "excludeTurns": True}
            )
        state = await self.thread_state(machine_id, thread_id)
        state["appliesTo"] = applies_to
        if save_defaults:
            self.remember_session_defaults(state["settings"], save_defaults)
        return state

    async def read_goal(self, machine_id: str, thread_id: str) -> dict:
        try:
            return await self.rpc(machine_id, "thread/goal/get", {"threadId": thread_id})
        except NexusError as error:
            if error.code != -32600 or "goals feature is disabled" not in str(error):
                raise
            self.sessions.goal_support[(machine_id, thread_id)] = False
            return {"goal": None, "supported": False}

    async def update_goal(self, machine_id: str, thread_id: str, body: dict) -> dict:
        async with self.turn_locks[(machine_id, thread_id)]:
            state = await self.thread_state(machine_id, thread_id)
            if state["thread"].get("archived"):
                await self.rpc(machine_id, "thread/unarchive", {"threadId": thread_id})
                await self.rpc(
                    machine_id, "thread/resume", {"threadId": thread_id, "excludeTurns": True}
                )
            params = {
                "threadId": thread_id,
                "origin": body.get("origin", "user"),
                **{key: body[key] for key in ("objective", "status", "tokenBudget") if key in body},
            }
            return await self.rpc(machine_id, "thread/goal/set", params)

    async def stop_goal(self, machine_id: str, thread_id: str, origin: str = "user") -> dict:
        async with self.turn_locks[(machine_id, thread_id)]:
            result = await self.rpc(
                machine_id,
                "thread/goal/set",
                {"threadId": thread_id, "status": "paused", "origin": origin},
            )
            if turn_id := await self.active_turn_id(machine_id, thread_id):
                await self.interrupt(machine_id, thread_id, turn_id)
            return result

    async def active_turn_id(self, machine_id: str, thread_id: str) -> str | None:
        cached = self.sessions.active_turns.get((machine_id, thread_id))
        if cached:
            return cached
        if (machine_id, thread_id) in self.sessions.fresh_threads:
            return None
        result = await self.rpc(
            machine_id,
            "thread/turns/list",
            {
                "threadId": thread_id,
                "limit": 1,
                "itemsView": "notLoaded",
                "sortDirection": "desc",
            },
        )
        for turn in result.get("data", []):
            if turn.get("status") == "inProgress":
                return turn["id"]
        return None

    async def send_message(self, machine_id: str, thread_id: str, body: dict) -> dict:
        async with self.turn_locks[(machine_id, thread_id)]:
            return await self._send_message(machine_id, thread_id, body)

    async def archive_thread(self, machine_id: str, thread_id: str, *, discard_empty: bool) -> dict:
        key = (machine_id, thread_id)
        params = {"threadId": thread_id}
        async with self.turn_locks[key]:
            fresh = self.sessions.fresh_threads.get(key)
            if discard_empty and fresh and not fresh["turns"] and not fresh["archived"]:
                await self.rpc(machine_id, "thread/delete", params)
                return {"deleted": True}
            return await self.rpc(machine_id, "thread/archive", params)

    async def resume_turn(self, machine_id: str, thread_id: str, turn_id: str) -> dict:
        async with self.turn_locks[(machine_id, thread_id)]:
            history = await self.history(machine_id, thread_id, limit=1)
            latest = next(iter(history["data"]), None)
            if not latest or latest["id"] != turn_id or latest["status"] != "interrupted":
                raise NexusError("只能恢复会话最后一个已中断的轮次", status_code=409)
            if history["thread"].get("archived"):
                await self.rpc(machine_id, "thread/unarchive", {"threadId": thread_id})
            return await self._send_message(machine_id, thread_id, {"input": []}, steer=False)

    async def _wait_status(self, machine_id: str, thread_id: str, turn_id: str | None) -> dict:
        thread = (await self.read_thread(machine_id, thread_id))["thread"]
        requests = await self.get_requests(machine_id, thread_id)
        if requests:
            return {"reason": "input_required", "thread": thread, "requests": requests}
        if thread.get("status", {}).get("type") == "systemError":
            return {"reason": "system_error", "thread": thread}
        if turn_id or thread.get("status", {}).get("type") in {"idle", "notLoaded"}:
            cursor = None
            while True:
                page = await self.history(machine_id, thread_id, cursor, 30 if turn_id else 1)
                if not turn_id:
                    return {"reason": "idle", "thread": thread, "history": page["data"]}
                turn = next((turn for turn in page["data"] if turn["id"] == turn_id), None)
                if turn:
                    if turn["status"] in {"completed", "failed", "interrupted"}:
                        return {"reason": "turn_completed", "thread": thread, "turn": turn}
                    break
                cursor = page.get("nextCursor")
                if not cursor:
                    break
        return {"reason": "running", "thread": thread, "requests": requests}

    async def wait_for_thread(
        self, machine_id: str, thread_id: str, timeout: float = 60, turn_id: str | None = None
    ) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        cursor = self.events.sequence
        iterator = self.events.subscribe(after=cursor, machine_id=machine_id)
        async with aclosing(iterator):
            await self.thread_state(machine_id, thread_id)
            state = await self._wait_status(machine_id, thread_id, turn_id)
            while state["reason"] == "running":
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return {**state, "reason": "timeout"}
                try:
                    event = await asyncio.wait_for(anext(iterator), remaining)
                except (TimeoutError, StopAsyncIteration):
                    return {**state, "reason": "timeout"}
                method, params = event["method"], event["params"]
                if method in {"nexus/machine/removed", "nexus/machine/updated"}:
                    if method.endswith("removed") or params["machine"]["status"] != "connected":
                        return {**state, "reason": "disconnected"}
                event_thread = params.get("threadId") or params.get("thread", {}).get("id")
                if method != "nexus/resync" and event_thread != thread_id:
                    continue
                if method in {
                    "turn/completed",
                    "nexus/request/created",
                    "thread/status/changed",
                    "thread/closed",
                    "nexus/resync",
                    "nexus/history/changed",
                }:
                    state = await self._wait_status(machine_id, thread_id, turn_id)
            return state

    async def _wait_turn_end(
        self, machine_id: str, thread_id: str, turn_id: str, after: int
    ) -> None:
        iterator = self.events.subscribe(after=after, machine_id=machine_id)
        async with aclosing(iterator), asyncio.timeout(15):
            while self.sessions.active_turns.get((machine_id, thread_id)) == turn_id:
                event = await anext(iterator)
                if (
                    event["method"] == "nexus/machine/updated"
                    and event["params"]["machine"]["status"] != "connected"
                ):
                    raise NexusError("机器连接已断开", status_code=503)

    async def edit_message(self, machine_id: str, thread_id: str, item_id: str, body: dict) -> dict:
        turn_id, text = body.get("turnId"), body.get("text")
        if not turn_id or not isinstance(text, str):
            raise NexusError("编辑消息需要 turnId 和 text")
        async with self.turn_locks[(machine_id, thread_id)]:
            await self.thread_state(machine_id, thread_id)
            cursor, inputs, original = None, [], None
            while original is None:
                page = await self.rpc(
                    machine_id,
                    "thread/items/list",
                    {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "cursor": cursor,
                        "limit": 100,
                        "sortDirection": "asc",
                    },
                )
                for entry in page["data"]:
                    item = entry["item"]
                    if item["id"] == item_id and item["type"] == "userMessage":
                        original = item
                        break
                    if item["type"] == "userMessage":
                        inputs.extend(deepcopy(item["content"]))
                cursor = page.get("nextCursor")
                if not cursor and original is None:
                    raise NexusError("找不到这条用户消息，请刷新会话", status_code=404)
            attachments = [deepcopy(part) for part in original["content"] if part["type"] != "text"]
            if not text.strip() and not attachments:
                raise NexusError("消息内容不能为空")
            prefix_length = len(inputs)
            inputs.extend(([{"type": "text", "text": text}] if text.strip() else []) + attachments)
            if active := await self.active_turn_id(machine_id, thread_id):
                after = self.events.sequence
                await self.interrupt(machine_id, thread_id, active)
                try:
                    await self._wait_turn_end(machine_id, thread_id, active, after)
                except TimeoutError:
                    raise NexusError("当前轮仍在停止中，请稍后重新发送", status_code=409) from None
            await self.rpc(
                machine_id, "thread/revert", {"threadId": thread_id, "beforeTurnId": turn_id}
            )
            self.sessions.active_turns.pop((machine_id, thread_id), None)
            try:
                return await self._send_message(
                    machine_id, thread_id, {**body, "input": inputs}, steer=False
                )
            except NexusError as exc:
                exc.data = {
                    "reverted": True,
                    "inputPrefix": inputs[:prefix_length],
                    "attachments": attachments,
                }
                raise

    async def regenerate_turn(
        self, machine_id: str, thread_id: str, turn_id: str, body: dict
    ) -> dict:
        async with self.turn_locks[(machine_id, thread_id)]:
            state = await self.thread_state(machine_id, thread_id)
            cursor = None
            while True:
                page = await self.rpc(
                    machine_id,
                    "thread/turns/list",
                    {
                        "threadId": thread_id,
                        "cursor": cursor,
                        "limit": 100,
                        "itemsView": "notLoaded",
                        "sortDirection": "desc",
                    },
                )
                if any(turn["id"] == turn_id for turn in page["data"]):
                    break
                cursor = page.get("nextCursor")
                if not cursor:
                    raise NexusError("找不到这个轮次，请刷新会话", status_code=404)
            if state["thread"].get("archived"):
                await self.rpc(machine_id, "thread/unarchive", {"threadId": thread_id})
                await self.rpc(
                    machine_id, "thread/resume", {"threadId": thread_id, "excludeTurns": True}
                )
            if active := await self.active_turn_id(machine_id, thread_id):
                after = self.events.sequence
                await self.interrupt(machine_id, thread_id, active)
                try:
                    await self._wait_turn_end(machine_id, thread_id, active, after)
                except TimeoutError:
                    raise NexusError("当前轮仍在停止中，请稍后重新生成", status_code=409) from None
            cursor, inputs, prefix_length, attachments = None, [], 0, []
            while True:
                page = await self.rpc(
                    machine_id,
                    "thread/items/list",
                    {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "cursor": cursor,
                        "limit": 100,
                        "sortDirection": "asc",
                        "deferOutput": {"commandExecution": True, "mcpToolCall": True},
                    },
                )
                for entry in page["data"]:
                    item = entry["item"]
                    if item["type"] == "userMessage":
                        prefix_length = len(inputs)
                        inputs.extend(deepcopy(item["content"]))
                        attachments = [
                            deepcopy(part) for part in item["content"] if part["type"] != "text"
                        ]
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            await self.rpc(
                machine_id, "thread/revert", {"threadId": thread_id, "beforeTurnId": turn_id}
            )
            self.sessions.active_turns.pop((machine_id, thread_id), None)
            try:
                return await self._send_message(
                    machine_id, thread_id, {**body, "input": inputs}, steer=False
                )
            except NexusError as exc:
                exc.data = {
                    "reverted": True,
                    "inputPrefix": inputs[:prefix_length],
                    "attachments": attachments,
                }
                raise

    async def regenerate_message(
        self, machine_id: str, thread_id: str, item_id: str, body: dict
    ) -> dict:
        turn_id = body.get("turnId")
        if not turn_id:
            raise NexusError("重新生成消息需要 turnId")
        async with self.turn_locks[(machine_id, thread_id)]:
            state = await self.thread_state(machine_id, thread_id)
            # Resolve the selected message before interrupting an unrelated active turn.
            prefix = await self.message_history.prefix(
                machine_id, thread_id, turn_id, item_id, inclusive=False
            )
            if state["thread"].get("archived"):
                await self.rpc(machine_id, "thread/unarchive", {"threadId": thread_id})
                await self.rpc(
                    machine_id, "thread/resume", {"threadId": thread_id, "excludeTurns": True}
                )
            if active := await self.active_turn_id(machine_id, thread_id):
                after = self.events.sequence
                await self.interrupt(machine_id, thread_id, active)
                try:
                    await self._wait_turn_end(machine_id, thread_id, active, after)
                except TimeoutError:
                    raise NexusError("当前轮仍在停止中，请稍后重新生成", status_code=409) from None
            async with self.message_history.rewrite(machine_id, thread_id):
                await self.rpc(
                    machine_id, "thread/revert", {"threadId": thread_id, "beforeTurnId": turn_id}
                )
                await self.message_history.append(machine_id, thread_id, prefix, state["settings"])
            return await self._send_message(
                machine_id, thread_id, {**body, "input": []}, steer=False
            )

    async def _send_message(
        self, machine_id: str, thread_id: str, body: dict, *, steer: bool = True
    ) -> dict:
        if (machine_id, thread_id) not in self.sessions.observed_threads and (
            machine_id,
            thread_id,
        ) not in self.sessions.fresh_threads:
            await self.rpc(
                machine_id, "thread/resume", {"threadId": thread_id, "excludeTurns": True}
            )
        input_items = body.get("input", [{"type": "text", "text": body.get("text", "")}])
        client_id = (body.get("clientUserMessageId") or str(uuid4())) if input_items else None
        actor, actor_name = body.get("actor", "human"), body.get("actorName")
        params = {"threadId": thread_id, "input": input_items}
        if client_id:
            await asyncio.to_thread(
                self.records.record_origin,
                machine_id,
                thread_id,
                client_id,
                actor,
                actor_name,
                body.get("delivery"),
            )
            params["clientUserMessageId"] = client_id
        active = await self.active_turn_id(machine_id, thread_id)
        if active and not steer:
            raise NexusError("会话已开始执行其他消息，请停止后重新发送", status_code=409)
        mode = "start"
        if active:
            mode = "steer"
            try:
                result = await self.rpc(
                    machine_id, "turn/steer", {**params, "expectedTurnId": active}
                )
            except NexusError as exc:
                if exc.code == -32600 and "no active turn" in exc.message:
                    self.sessions.active_turns.pop((machine_id, thread_id), None)
                    mode = "start"
                elif exc.code == -32600 and (match := re.search(r"found `([^`]+)`", exc.message)):
                    result = await self.rpc(
                        machine_id, "turn/steer", {**params, "expectedTurnId": match[1]}
                    )
                else:
                    raise
        if mode == "start":
            current = self.sessions.thread_settings.get((machine_id, thread_id), {})
            overrides = {
                native: current[field]
                for native, field in (
                    ("model", "model"),
                    ("effort", "reasoningEffort"),
                    ("serviceTier", "serviceTier"),
                )
                if field in current
            }
            overrides.update(
                {
                    field: body[field]
                    for field in (
                        "model",
                        "effort",
                        "serviceTier",
                        "collaborationMode",
                        "approvalPolicy",
                        "approvalsReviewer",
                        "sandboxPolicy",
                        "permissions",
                        "cwd",
                        "summary",
                        "outputSchema",
                        "serviceTierForTurn",
                    )
                    if field in body
                }
            )
            if "reasoningEffort" in body:
                overrides["effort"] = body["reasoningEffort"]
            result = await self.rpc(machine_id, "turn/start", {**params, **overrides})
        turn_id = result.get("turnId") or result["turn"]["id"]
        await self.events.publish(
            machine_id,
            "nexus/message/submitted",
            {
                "threadId": thread_id,
                "turnId": turn_id,
                "mode": mode,
                "clientUserMessageId": client_id,
                "actor": actor,
                "actorName": actor_name,
            },
        )
        return {"turnId": turn_id, "mode": mode}

    async def interrupt(self, machine_id: str, thread_id: str, turn_id: str | None = None) -> dict:
        turn_id = turn_id or await self.active_turn_id(machine_id, thread_id)
        if not turn_id:
            raise NexusError("当前会话没有正在执行的轮次", status_code=409)
        return await self.rpc(
            machine_id, "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
        )

    async def get_requests(
        self, machine_id: str | None = None, thread_id: str | None = None
    ) -> list[dict]:
        return [
            request
            for request in self.pending_requests.values()
            if (not machine_id or request["machineId"] == machine_id)
            and (not thread_id or request["threadId"] == thread_id)
        ]

    async def respond_request(self, key: str, result: dict) -> dict:
        async with self.reply_locks[key]:
            if key not in self.pending_requests:
                raise NexusError("该请求已结束", status_code=404)
            request = self.pending_requests[key]
            if self.demo_network:
                await self.demo_network.respond(
                    request["machineId"], self.native_request_ids[key], result
                )
            else:
                await self.runtime.connections[request["machineId"]].respond(
                    self.native_request_ids[key], result
                )
            if key in self.pending_requests:
                await self._resolve_request(key)
            return {"resolved": True}
