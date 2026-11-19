"""HTTP and event interfaces shared by the Web UI and automation clients."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import Body, FastAPI, Form, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import __version__
from .access import EntryAccessMiddleware, entry_security
from .commands import CommandRequest, ScreenshotRequest
from .context_usage import context_usage
from .core import NexusError, SessionService
from .developer_inputs import developer_inputs
from .files import FileCreate, FileRename, TextFileEdit, file_info
from .mcp_server import create_mcp_server
from .messaging import bot_id
from .network import entry_urls, https_url
from .terminals import TerminalRequest


def create_app(
    data_dir: Path | None = None,
    demo: bool = False,
    nexus_url: str = "http://127.0.0.1:8765",
    tailscale_only: bool = False,
    static_dir: Path | None = None,
    allowed_urls: list[str] | None = None,
    demo_config: Path | None = None,
) -> FastAPI:
    security = entry_security(entry_urls(nexus_url, allowed_urls or []))
    directory = data_dir or Path(
        os.environ.get("NEXUS_DATA_DIR", Path.home() / ".local/share/nexus")
    )
    service = SessionService(
        Path(directory), demo=demo, nexus_url=nexus_url, demo_config=demo_config
    )
    mcp = create_mcp_server(nexus_url)
    mcp_app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host="0.0.0.0",
        transport_security=security,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with mcp.session_manager.run():
            await service.start()
            try:
                yield
            finally:
                await service.close()

    app = FastAPI(
        title="Nexus",
        description="Control Codex sessions across your machines.",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.service = service
    app.add_middleware(EntryAccessMiddleware, security=security)

    @app.exception_handler(NexusError)
    async def handle_nexus_error(request: Request, error: NexusError):
        detail = {"message": str(error)}
        if error.code is not None:
            detail["code"] = error.code
        if error.data is not None:
            detail["data"] = error.data
        return JSONResponse({"error": detail}, status_code=error.status_code)

    @app.get("/api/config")
    async def config():
        return {
            "app": "Nexus",
            "version": __version__,
            "demo": service.demo,
            "features": service.tool_context.features(),
        }

    @app.get("/api/features")
    async def features():
        return service.tool_context.features()

    @app.patch("/api/features")
    async def update_features(body: dict = Body(...)):
        return await service.tool_context.update_features(body)

    @app.api_route("/api/tool-context/{token}/{path:path}", methods=["GET", "POST"])
    async def tool_context_proxy(token: str, path: str, request: Request):
        return await service.tool_context.proxy(token, path, request)

    @app.websocket("/api/tool-context/{token}/{path:path}")
    async def tool_context_http_transport(websocket: WebSocket, token: str, path: str):
        await websocket.send_denial_response(Response(status_code=426))

    @app.get("/api/session-defaults")
    async def session_defaults():
        return service.store.session_defaults

    @app.get("/api/health")
    async def health():
        state = service.health()
        return JSONResponse(state, status_code=200 if state["status"] == "ok" else 503)

    @app.get("/api/push/key")
    async def push_key():
        return {"publicKey": service.push.public_key()}

    @app.post("/api/push/subscriptions")
    async def subscribe_push(request: Request, body: dict = Body(...)):
        service.push.public_key()
        subject = os.environ.get("NEXUS_PUSH_SUBJECT") or str(request.base_url).rstrip("/")
        service.push.subscribe(body, subject)
        service.observe_loaded()
        return {"subscribed": True}

    @app.delete("/api/push/subscriptions")
    async def unsubscribe_push(body: dict = Body(...)):
        service.push.unsubscribe(body["endpoint"])
        return {"subscribed": False}

    @app.post("/api/push/test")
    async def test_push(body: dict = Body(...)):
        await service.push.send(
            body["endpoint"],
            {
                "title": "Nexus 测试通知",
                "body": "任务完成后会在这里提醒你。",
                "tag": f"nexus-test-{body.get('testId', 'test')}",
                "url": "/",
                "testId": body.get("testId"),
            },
        )
        return {"sent": True}

    @app.get("/api/machines")
    async def machines():
        return {"machines": await service.machines()}

    @app.get("/api/accounts/usage")
    async def account_usage(refresh: bool = False):
        return JSONResponse(
            await service.accounts.read(refresh=refresh), headers={"Cache-Control": "no-store"}
        )

    @app.get("/api/threads/resolve")
    async def resolve_thread(threadId: str, machineId: str | None = None):
        return await service.messaging.resolve(threadId, machineId)

    @app.post("/api/messages")
    async def send_agent_message(body: dict = Body(...)):
        return await service.messaging.send(body)

    @app.get("/api/groups")
    async def groups():
        groups = service.messaging.groups()
        return {
            "groups": groups,
            "creationOrder": list(groups),
            "summaries": service.messaging.messages.summaries(),
        }

    @app.get("/api/bots")
    async def bots():
        return {"data": await service.messaging.bots()}

    @app.put("/api/machines/{machine_id}/threads/{thread_id}/groups")
    async def set_thread_groups(
        machine_id: str, thread_id: str, names: list[str] = Body(..., embed=True)
    ):
        return await service.messaging.set_thread_groups(machine_id, thread_id, names)

    @app.get("/api/groups/{name}/messages")
    async def group_messages(
        name: str, before: int | None = None, limit: int = Query(50, ge=1, le=200)
    ):
        return service.messaging.history(name, before, limit)

    @app.post("/api/groups/{name}/messages")
    async def send_group_user_message(name: str, body: dict = Body(...)):
        if not body.get("text", "").strip():
            raise NexusError("请填写消息")
        return await service.messaging.send(
            {
                "groupName": name,
                "senderKind": "human",
                "text": body["text"],
                "clientMessageId": body.get("clientMessageId"),
            }
        )

    @app.put("/api/groups/{name}")
    async def set_group(
        name: str, thread_ids: list[str] = Body(..., embed=True, alias="threadIds")
    ):
        return await service.messaging.set_group(name, thread_ids)

    @app.delete("/api/groups/{name}")
    async def delete_group(name: str):
        return await service.messaging.delete_group(name)

    async def run_command(request: Request, operation):
        command = asyncio.create_task(operation)

        async def wait_for_disconnect():
            while (await request.receive())["type"] != "http.disconnect":
                pass

        disconnected = asyncio.create_task(wait_for_disconnect())
        try:
            done, _ = await asyncio.wait(
                {command, disconnected}, return_when=asyncio.FIRST_COMPLETED
            )
            if command in done:
                return await command
            raise NexusError("命令请求已取消", status_code=499)
        finally:
            command.cancel()
            disconnected.cancel()
            await asyncio.gather(command, disconnected, return_exceptions=True)

    @app.post("/api/machines/{machine_id}/commands")
    async def execute_command(machine_id: str, body: CommandRequest, request: Request):
        return await run_command(
            request,
            service.commands.execute(
                machine_id,
                body.command,
                body.cwd,
                body.timeout_seconds,
            ),
        )

    @app.post("/api/machines/{machine_id}/screenshot")
    async def capture_screenshot(machine_id: str, body: ScreenshotRequest, request: Request):
        png, path = await run_command(
            request,
            service.commands.screenshot(
                machine_id,
                body.serial,
                body.timeout_seconds,
                include_path=body.include_path,
            ),
        )
        headers = {"Cache-Control": "no-store"}
        if path is not None:
            headers["X-Nexus-Screenshot-Path"] = quote(path)
        return Response(png, media_type="image/png", headers=headers)

    @app.get("/api/machines/{machine_id}/phone-numbers")
    async def get_phone_numbers(
        machine_id: str,
        request: Request,
        serial: str | None = None,
        timeoutSeconds: float = Query(15, gt=0),
    ):
        result = await run_command(
            request,
            service.commands.phone_numbers(
                machine_id,
                serial,
                timeoutSeconds,
            ),
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.get("/api/terminals")
    async def list_terminals():
        return {"data": service.terminals.list()}

    @app.post("/api/machines/{machine_id}/terminals", status_code=201)
    async def create_terminal(machine_id: str, body: TerminalRequest):
        return await service.terminals.create(machine_id, body)

    @app.delete("/api/terminals/{terminal_id}", status_code=204)
    async def delete_terminal(terminal_id: str):
        await service.terminals.delete(terminal_id)

    @app.websocket("/api/terminals/{terminal_id}/stream")
    async def terminal_stream(websocket: WebSocket, terminal_id: str, after: int = 0):
        try:
            terminal = service.terminals.get(terminal_id)
        except NexusError:
            await websocket.close(code=1008, reason="终端已关闭")
            return
        await websocket.accept()
        try:
            await terminal.stream(websocket, after)
        except WebSocketDisconnect:
            pass
        finally:
            with suppress(RuntimeError, OSError, WebSocketDisconnect):
                await websocket.close()

    @app.post("/api/machines", status_code=201)
    async def add_machine(body: dict = Body(...)):
        return await service.add_machine(body)

    @app.patch("/api/machines/{machine_id}")
    async def update_machine(machine_id: str, body: dict = Body(...)):
        return await service.update_machine(machine_id, body)

    @app.delete("/api/machines/{machine_id}")
    async def remove_machine(machine_id: str):
        await service.remove_machine(machine_id)
        return {"removed": True}

    @app.post("/api/machines/{machine_id}/projects", status_code=201)
    async def add_project(machine_id: str, body: dict = Body(...)):
        return await service.add_project(
            machine_id, body.get("cwd", ""), create_directory=body.get("createDirectory") is True
        )

    @app.delete("/api/machines/{machine_id}/projects")
    async def remove_project(machine_id: str, body: dict = Body(...)):
        return await service.remove_project(machine_id, body.get("cwd", ""))

    @app.get("/api/machines/{machine_id}/directories")
    async def directories(machine_id: str, path: str = "~"):
        return await service.directories(machine_id, path)

    @app.get("/api/machines/{machine_id}/files")
    async def list_files(
        machine_id: str,
        path: str = "~",
        searchTerm: str = "",
        mode: Literal["title", "content", "both"] = "title",
    ):
        return await service.files.list(machine_id, path, searchTerm, mode)

    @app.post("/api/machines/{machine_id}/files")
    async def upload_files(
        machine_id: str,
        files: list[UploadFile],
        directory: str = Form(...),
        overwrite: bool = Form(False),
    ):
        return await service.files.upload(machine_id, directory, files, overwrite)

    @app.put("/api/machines/{machine_id}/files/text")
    async def edit_text_file(machine_id: str, body: TextFileEdit):
        return await service.files.edit_text(machine_id, body)

    @app.post("/api/machines/{machine_id}/files/create", status_code=201)
    async def create_file(machine_id: str, body: FileCreate):
        return await service.files.create(machine_id, body)

    @app.post("/api/machines/{machine_id}/files/rename")
    async def rename_file(machine_id: str, body: FileRename):
        return await service.files.rename(machine_id, body)

    @app.get("/api/machines/{machine_id}/files/download")
    async def download_file(machine_id: str, path: str, preview: bool = False):
        local, staging = await service.files.download(machine_id, path)
        info = file_info(local)
        return FileResponse(
            local,
            filename=info["name"],
            media_type=info["mimeType"],
            content_disposition_type="inline"
            if preview and info["mimeType"].startswith("image/")
            else "attachment",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox",
            },
            background=BackgroundTask(staging.cleanup) if staging else None,
        )

    @app.post("/api/files/transfer")
    async def transfer_files(body: dict = Body(...)):
        return await service.files.transfer(
            body["sourceMachineId"],
            body["sourcePaths"],
            body["targetMachineId"],
            body["targetDirectory"],
            recursive=body.get("recursive", False),
            overwrite=body.get("overwrite", False),
        )

    @app.post("/api/machines/{machine_id}/connect")
    async def connect_machine(machine_id: str):
        return await service.connect_machine(machine_id)

    @app.post("/api/machines/{machine_id}/disconnect")
    async def disconnect_machine(machine_id: str):
        return await service.disconnect_machine(machine_id)

    @app.post("/api/machines/{machine_id}/reconnect")
    async def reconnect_machine(machine_id: str):
        return await service.reconnect_machine(machine_id)

    @app.get("/api/search")
    async def search_threads(
        searchTerm: str = Query(min_length=1),
        mode: Literal["title", "content", "both", "id"] = "title",
        view: Literal["recent", "working", "waiting"] = "recent",
        machineId: str | None = None,
        cwd: str | None = None,
        archived: bool | None = None,
        cursor: str | None = None,
        limit: int = Query(default=10, ge=1),
    ):
        return await service.search_threads(
            searchTerm,
            mode,
            view,
            machine_id=machineId,
            cwd=cwd,
            archived=archived,
            cursor=cursor,
            limit=limit,
        )

    @app.post("/api/search/sync")
    async def sync_search(machineId: str | None = None):
        machines = [service.machine(machineId)] if machineId else await service.machines()
        for machine in machines:
            service.search_sync.wake(machine["id"])
        return {"syncing": any(machine["id"] in service.search_sync.tasks for machine in machines)}

    @app.get("/api/threads")
    async def threads(
        machineId: str | None = None,
        scope: str = "loaded",
        cwd: str | None = None,
        archived: bool | None = None,
        searchTerm: str | None = None,
        limit: int = Query(default=50, ge=1),
    ):
        params = {"scope": scope, "archived": archived, "limit": limit}
        if cwd:
            params["cwd"] = cwd
        if searchTerm:
            params["searchTerm"] = searchTerm
        result = await service.list_threads(machineId, params)
        if machineId:
            return {
                "data": result["data"],
                "errors": [],
                "nextCursors": {machineId: result["nextCursor"]}
                if result.get("nextCursor")
                else {},
            }
        return result

    @app.get("/api/machines/{machine_id}/threads")
    async def machine_threads(
        machine_id: str,
        scope: str = "loaded",
        cwd: str | None = None,
        archived: bool | None = None,
        searchTerm: str | None = None,
        cursor: str | None = None,
        limit: int = Query(default=50, ge=1),
    ):
        params = {"scope": scope, "archived": archived, "limit": limit}
        if cwd:
            params["cwd"] = cwd
        if cursor:
            params["cursor"] = cursor
        if searchTerm:
            params["searchTerm"] = searchTerm
        return await service.list_threads(machine_id, params)

    @app.post("/api/machines/{machine_id}/threads", status_code=201)
    async def create_thread(machine_id: str, body: dict = Body(...)):
        params = {"threadSource": "nexus", **body}
        name = params.pop("name", None)
        default_fields = params.pop("saveDefaults", [])
        effort = params.pop("effort", params.pop("reasoningEffort", None))
        if effort is not None:
            params["config"] = {**(params.get("config") or {}), "model_reasoning_effort": effort}
        await service.apply_session_defaults(machine_id, params)
        result = await service.rpc(machine_id, "thread/start", params)
        result["thread"]["botId"] = bot_id(result["thread"]["id"])
        await service.add_project(machine_id, result["thread"]["cwd"])
        if name:
            await service.rpc(
                machine_id,
                "thread/name/set",
                {"threadId": result["thread"]["id"], "name": name},
            )
            result["thread"]["name"] = name
        if default_fields:
            service.remember_session_defaults(result, default_fields)
        return {**result, "machineId": machine_id}

    @app.get("/api/machines/{machine_id}/threads/{thread_id}")
    async def read_thread(machine_id: str, thread_id: str):
        return await service.read_thread(machine_id, thread_id)

    @app.get("/api/machines/{machine_id}/threads/{thread_id}/history")
    async def history(
        machine_id: str,
        thread_id: str,
        cursor: str | None = None,
        limit: int = 30,
    ):
        return await service.history(machine_id, thread_id, cursor=cursor, limit=limit)

    @app.get("/api/machines/{machine_id}/threads/{thread_id}/goal")
    async def read_goal(machine_id: str, thread_id: str):
        return await service.read_goal(machine_id, thread_id)

    @app.put("/api/machines/{machine_id}/threads/{thread_id}/goal")
    async def update_goal(machine_id: str, thread_id: str, body: dict = Body(...)):
        return await service.update_goal(machine_id, thread_id, body)

    @app.post("/api/machines/{machine_id}/threads/{thread_id}/goal/resume")
    async def resume_goal(machine_id: str, thread_id: str, body: dict = Body(default={})):
        return await service.update_goal(machine_id, thread_id, {**body, "status": "active"})

    @app.post("/api/machines/{machine_id}/threads/{thread_id}/goal/stop")
    async def stop_goal(machine_id: str, thread_id: str, body: dict = Body(default={})):
        return await service.stop_goal(machine_id, thread_id, body.get("origin", "user"))

    @app.delete("/api/machines/{machine_id}/threads/{thread_id}/goal")
    async def clear_goal(machine_id: str, thread_id: str, body: dict = Body(default={})):
        async with service.turn_locks[(machine_id, thread_id)]:
            return await service.rpc(
                machine_id,
                "thread/goal/clear",
                {"threadId": thread_id, "origin": body.get("origin", "user")},
            )

    @app.get("/api/machines/{machine_id}/threads/{thread_id}/state")
    async def thread_state(machine_id: str, thread_id: str):
        return await service.thread_state(machine_id, thread_id)

    @app.get("/api/machines/{machine_id}/threads/{thread_id}/developer-inputs")
    async def thread_developer_inputs(machine_id: str, thread_id: str, preview: bool = False):
        return await developer_inputs(service, machine_id, thread_id, preview=preview)

    @app.get("/api/machines/{machine_id}/threads/{thread_id}/context-usage")
    async def thread_context_usage(machine_id: str, thread_id: str):
        return await context_usage(service, machine_id, thread_id)

    @app.get("/api/machines/{machine_id}/threads/{thread_id}/snapshot")
    async def thread_snapshot(
        machine_id: str, thread_id: str, limit: int = Query(default=30, ge=1)
    ):
        return await service.snapshot(machine_id, thread_id, limit)

    @app.get("/api/machines/{machine_id}/threads/{thread_id}/wait")
    async def wait_thread(
        machine_id: str,
        thread_id: str,
        timeoutSeconds: float = Query(default=60, ge=0),
        turnId: str | None = None,
    ):
        return await service.wait_for_thread(machine_id, thread_id, timeoutSeconds, turnId)

    @app.patch("/api/machines/{machine_id}/threads/{thread_id}/settings")
    async def thread_settings(machine_id: str, thread_id: str, body: dict = Body(...)):
        settings = dict(body)
        default_fields = settings.pop("saveDefaults", [])
        return await service.update_thread_settings(machine_id, thread_id, settings, default_fields)

    @app.post("/api/machines/{machine_id}/threads/{thread_id}/compact")
    async def compact_thread(machine_id: str, thread_id: str):
        return await service.rpc(machine_id, "thread/compact/start", {"threadId": thread_id})

    @app.post("/api/machines/{machine_id}/threads/{thread_id}/review")
    async def review_thread(machine_id: str, thread_id: str, body: dict = Body(default={})):
        return await service.rpc(
            machine_id,
            "review/start",
            {"target": {"type": "uncommittedChanges"}, **body, "threadId": thread_id},
        )

    @app.get("/api/machines/{machine_id}/threads/{thread_id}/skills")
    async def skills(machine_id: str, thread_id: str):
        state = await service.thread_state(machine_id, thread_id)
        return await service.rpc(machine_id, "skills/list", {"cwds": [state["thread"]["cwd"]]})

    @app.get("/api/machines/{machine_id}/threads/{thread_id}/mcp")
    async def mcp_servers(machine_id: str, thread_id: str):
        return await service.rpc(machine_id, "mcpServerStatus/list", {"threadId": thread_id})

    @app.get("/api/machines/{machine_id}/threads/{thread_id}/diff")
    async def workspace_diff(machine_id: str, thread_id: str):
        state = await service.thread_state(machine_id, thread_id)
        cwd = state["thread"]["cwd"]

        async def git(arguments: list[str]):
            return await service.rpc(
                machine_id, "command/exec", {"command": ["git", *arguments], "cwd": cwd}
            )

        tracked, untracked = await asyncio.gather(
            git(
                [
                    "diff",
                    "--no-textconv",
                    "--no-ext-diff",
                    "--submodule=short",
                    "--ignore-submodules=dirty",
                    "--no-color",
                ]
            ),
            git(["ls-files", "--others", "--exclude-standard"]),
        )
        if tracked["exitCode"]:
            raise NexusError(tracked["stderr"], status_code=400)
        additions = await asyncio.gather(
            *(
                git(
                    [
                        "diff",
                        "--no-index",
                        "--no-textconv",
                        "--no-ext-diff",
                        "--no-color",
                        "--",
                        "/dev/null",
                        path,
                    ]
                )
                for path in untracked["stdout"].splitlines()
            )
        )
        return {
            "diff": "\n".join(result["stdout"] for result in [tracked, *additions]),
            "exitCode": 0,
        }

    @app.post("/api/machines/{machine_id}/threads/{thread_id}/messages")
    async def send_message(machine_id: str, thread_id: str, body: dict = Body(...)):
        return await service.send_message(machine_id, thread_id, body)

    @app.post("/api/machines/{machine_id}/threads/{thread_id}/messages/{item_id}/edit")
    async def edit_message(machine_id: str, thread_id: str, item_id: str, body: dict = Body(...)):
        return await service.edit_message(machine_id, thread_id, item_id, body)

    @app.post("/api/machines/{machine_id}/threads/{thread_id}/messages/{item_id}/regenerate")
    async def regenerate_message(
        machine_id: str, thread_id: str, item_id: str, body: dict = Body(...)
    ):
        return await service.regenerate_message(machine_id, thread_id, item_id, body)

    @app.post("/api/machines/{machine_id}/threads/{thread_id}/turns/{turn_id}/regenerate")
    async def regenerate_turn(
        machine_id: str, thread_id: str, turn_id: str, body: dict = Body(default={})
    ):
        return await service.regenerate_turn(machine_id, thread_id, turn_id, body)

    @app.post("/api/machines/{machine_id}/threads/{thread_id}/resume")
    async def resume_turn(
        machine_id: str, thread_id: str, turn_id: str = Body(..., embed=True, alias="turnId")
    ):
        return await service.resume_turn(machine_id, thread_id, turn_id)

    @app.post("/api/machines/{machine_id}/threads/{thread_id}/fork", status_code=201)
    async def fork_thread(machine_id: str, thread_id: str, body: dict = Body(default={})):
        result = await service.fork_thread(machine_id, thread_id, {**body, "excludeTurns": True})
        await service.add_project(machine_id, result["thread"]["cwd"])
        return {**result, "machineId": machine_id}

    @app.get("/api/machines/{machine_id}/threads/{thread_id}/sides")
    async def side_conversations(machine_id: str, thread_id: str):
        return await service.sides.list(machine_id, thread_id)

    @app.post("/api/machines/{machine_id}/threads/{thread_id}/sides", status_code=201)
    async def create_side_conversation(machine_id: str, thread_id: str):
        return await service.sides.create(machine_id, thread_id)

    @app.delete("/api/machines/{machine_id}/threads/{thread_id}/sides/{side_id}")
    async def delete_side_conversation(machine_id: str, thread_id: str, side_id: str):
        return await service.sides.delete(machine_id, thread_id, side_id)

    @app.post("/api/machines/{machine_id}/threads/{thread_id}/interrupt")
    async def interrupt_turn(machine_id: str, thread_id: str, body: dict = Body(default={})):
        return await service.interrupt(machine_id, thread_id, body.get("turnId"))

    @app.post("/api/machines/{machine_id}/threads/{thread_id}/archive")
    async def archive_thread(
        machine_id: str,
        thread_id: str,
        discard_empty: bool = Body(default=False, embed=True, alias="discardEmpty"),
    ):
        return await service.archive_thread(machine_id, thread_id, discard_empty=discard_empty)

    @app.post("/api/machines/{machine_id}/threads/{thread_id}/unarchive")
    async def unarchive_thread(machine_id: str, thread_id: str):
        return await service.rpc(machine_id, "thread/unarchive", {"threadId": thread_id})

    @app.post("/api/machines/{machine_id}/threads/{thread_id}/rename")
    async def rename_thread(machine_id: str, thread_id: str, body: dict = Body(...)):
        return await service.rpc(
            machine_id, "thread/name/set", {"threadId": thread_id, "name": body["name"]}
        )

    @app.get("/api/machines/{machine_id}/threads/{thread_id}/background-tasks")
    async def background_tasks(
        machine_id: str, thread_id: str, cursor: str | None = None, limit: int = 50
    ):
        params = {"threadId": thread_id, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await service.rpc(machine_id, "thread/backgroundTerminals/list", params)

    @app.post(
        "/api/machines/{machine_id}/threads/{thread_id}/background-tasks/{process_id}/terminate"
    )
    async def terminate_task(machine_id: str, thread_id: str, process_id: str):
        return await service.rpc(
            machine_id,
            "thread/backgroundTerminals/terminate",
            {"threadId": thread_id, "processId": process_id},
        )

    @app.post("/api/machines/{machine_id}/threads/{thread_id}/background-tasks/clean")
    async def clean_tasks(machine_id: str, thread_id: str):
        return await service.rpc(
            machine_id, "thread/backgroundTerminals/clean", {"threadId": thread_id}
        )

    @app.get("/api/requests")
    async def requests(machineId: str | None = None, threadId: str | None = None):
        return {"data": await service.get_requests(machineId, threadId)}

    @app.post("/api/requests/{request_id}/respond")
    async def respond_request(request_id: str, body: dict = Body(...)):
        return await service.respond_request(request_id, body["result"])

    @app.get("/api/machines/{machine_id}/models")
    async def models(machine_id: str):
        return await service.rpc(machine_id, "model/list", {})

    @app.post("/api/machines/{machine_id}/rpc")
    async def rpc(machine_id: str, body: dict = Body(...)):
        return {"result": await service.rpc(machine_id, body["method"], body.get("params", {}))}

    @app.get("/api/events")
    async def events(
        request: Request,
        machineId: str | None = None,
        threadId: str | None = None,
        after: int | None = None,
        epoch: str | None = None,
    ):
        last_id = request.headers.get("last-event-id")
        if after is None and last_id:
            if ":" in last_id:
                epoch, last_id = last_id.rsplit(":", 1)
            after = int(last_id)

        async def stream():
            iterator = service.events.subscribe(
                after=after,
                machine_id=machineId,
                thread_id=threadId,
                epoch=epoch,
            )
            pending = asyncio.create_task(anext(iterator))
            try:
                yield ": connected\n\n"
                while True:
                    ready, _ = await asyncio.wait({pending}, timeout=15)
                    if not ready:
                        yield ": keepalive\n\n"
                        continue
                    try:
                        event = pending.result()
                    except StopAsyncIteration:
                        return
                    yield (
                        f"id: {event['epoch']}:{event['id']}\nevent: nexus\n"
                        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    )
                    pending = asyncio.create_task(anext(iterator))
            finally:
                pending.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration):
                    await pending
                await iterator.aclose()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    static_dir = static_dir or Path(__file__).parent / "static"

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        return FileResponse(
            static_dir / "manifest.webmanifest", media_type="application/manifest+json"
        )

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        return FileResponse(
            static_dir / "sw.js",
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/offline.html", include_in_schema=False)
    async def offline():
        return FileResponse(static_dir / "offline.html")

    app.mount("/static", StaticFiles(directory=static_dir, check_dir=False), name="static")

    @app.get("/", include_in_schema=False)
    async def index(request: Request):
        if tailscale_only and request.url.scheme == "http":
            origin = await asyncio.to_thread(https_url, request.scope["server"][1])
            if origin:
                target = f"{origin}/" + (f"?{request.url.query}" if request.url.query else "")
                return RedirectResponse(target, headers={"Cache-Control": "no-cache"})
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/", mcp_app)
    return app
