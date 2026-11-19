"""MCP tools over stdio and HTTP, backed by the Web interface's HTTP API."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from urllib.parse import quote, unquote

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver import Context, Image
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from . import __version__
from .network import default_url

ArchiveStatus = Literal["all", "unarchived", "archived"]
ARCHIVE_FILTERS = {"all": None, "unarchived": "false", "archived": "true"}


def _machine_path(machine_id: str) -> str:
    return f"/api/machines/{quote(machine_id, safe='')}"


def _thread_path(machine_id: str, thread_id: str) -> str:
    return f"{_machine_path(machine_id)}/threads/{quote(thread_id, safe='')}"


def _params(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _caller_thread(ctx: Context) -> str:
    thread_id = (ctx.request_context.meta or {}).get("threadId")
    if not thread_id:
        raise ToolError("工具调用缺少 Codex 会话身份（_meta.threadId）")
    return thread_id


class NexusAPI:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")

    async def request(
        self, method: str, path: str, *, raw: bool = False, **kwargs: Any
    ) -> dict[str, Any] | httpx.Response:
        timeout = kwargs.pop("timeout", 60)
        try:
            async with httpx.AsyncClient(
                base_url=self.url, timeout=timeout, trust_env=False
            ) as client:
                response = await client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise ToolError(f"无法连接 Nexus 服务：{exc}") from exc
        if response.is_error:
            try:
                payload = response.json()
                message = payload.get("error", {}).get("message") or response.text
            except ValueError:
                message = response.text
            raise ToolError(f"Nexus HTTP {response.status_code}: {message}")
        if raw:
            return response
        return response.json() if response.content else {}


def create_mcp_server(url: str | None = None) -> MCPServer:
    api = NexusAPI(url or default_url())
    server = MCPServer(
        "nexus",
        version=__version__,
        instructions=(
            "Manage Codex sessions across machines through one Nexus service. "
            "Use machine IDs from list_machines. Native Codex IDs and parameters are preserved. "
            "execute_command runs a command directly on a registered machine, independently of Codex. "
            "capture_screenshot returns an Android screenshot as an image using adb on the named machine. "
            "get_phone_numbers reads active SIM phone numbers using adb on the named machine. "
            "transfer_files copies files between registered machines through the Nexus host; both paths belong to the named machines. "
            "Use add_project and remove_project to change tracked directories without replacing projects. "
            "list_threads discovers saved history, including archived and unloaded conversations by default. "
            "Each bot uses the last six characters of its thread ID as botId. "
            "whoami reads your identity automatically from the Codex request context. "
            "resolve_thread expands a botId into machineId and the full threadId. "
            "send_message starts a turn when idle and steers the current turn while running. "
            "It identifies the sender automatically; supply one recipient, a recipient list, or a group_name. "
            "Use small recipient lists for targeted collaboration; use a group only when all members need the message. "
            "A list sends independent messages; cc=true shows each recipient the other recipients. "
            "set_group saves a named recipient list and list_groups reads group membership. "
            "To reply publicly in a group, use send_message with group_name; ordinary assistant output stays in your own conversation. "
            "interrupt_turn and background task termination are separate operations. "
            "fork_thread accepts turn boundaries or turn_id with last_item_id for an assistant-message boundary. "
            "Forking a side conversation creates another side conversation under the same main thread. "
            "regenerate_message replaces an assistant message and later history, retaining its earlier context. "
            "regenerate_turn restarts an entire turn with its original user inputs and attachments. "
            "edit_message replaces a user message and subsequent history in the same thread. "
            "Pending native approval and question requests are answered with respond_request."
        ),
    )

    @server.tool()
    async def list_machines() -> dict[str, Any]:
        """List machines, their IDs, connection status, and available projects."""
        return await api.request("GET", "/api/machines")

    @server.tool()
    async def execute_command(
        machine_id: str,
        command: Annotated[list[str], Field(min_length=1)],
        cwd: str,
        timeout_seconds: Annotated[float, Field(gt=0)] = 60,
    ) -> dict[str, Any]:
        """Run a one-shot command directly on a registered machine, independently of Codex.

        command is an argv array, e.g. ["git", "status", "--short"]. For shell syntax,
        explicitly invoke a shell, e.g. ["bash", "-lc", "pwd && ls"]. cwd is an absolute
        directory on the target machine. Runs as the Nexus process user locally or the
        SSH login user remotely. Remote execution requires Bash. Returns exitCode, stdout,
        stderr, timedOut, stdoutTruncated, and stderrTruncated. Each output stream captures
        up to 1 MiB. Timeout defaults to 60 seconds and stops the command's process group.
        """
        return await api.request(
            "POST",
            f"{_machine_path(machine_id)}/commands",
            json={
                "command": command,
                "cwd": cwd,
                "timeoutSeconds": timeout_seconds,
            },
            timeout=timeout_seconds + 30,
        )

    @server.tool(structured_output=False)
    async def capture_screenshot(
        machine_id: str,
        include_path: bool = True,
        serial: str | None = None,
        timeout_seconds: Annotated[float, Field(gt=0)] = 15,
    ) -> list[Image | str]:
        """Capture an Android screen and return the PNG as native MCP image content.

        Runs adb exec-out screencap -p on the named machine, independently of Codex.
        For a phone running Nexus commands in Termux, adb must be connected to that phone.
        By default also saves the PNG in that machine's cache and returns its absolute path
        for file operations. Set include_path=false to return only the image without saving.
        Omit serial when adb has a single device; otherwise use a serial from adb devices.
        """
        response = await api.request(
            "POST",
            f"{_machine_path(machine_id)}/screenshot",
            raw=True,
            json=_params(includePath=include_path, serial=serial, timeoutSeconds=timeout_seconds),
            timeout=timeout_seconds + 30,
        )
        content: list[Image | str] = [Image(data=response.content, format="png")]
        if include_path:
            path = unquote(response.headers["X-Nexus-Screenshot-Path"])
            content.append(f"Screenshot saved on machine {machine_id}: {path}")
        return content

    @server.tool()
    async def get_phone_numbers(
        machine_id: str,
        serial: str | None = None,
        timeout_seconds: Annotated[float, Field(gt=0)] = 15,
    ) -> dict[str, Any]:
        """Read active SIM phone numbers on an Android execution machine using adb.

        Requires Android 13+ and adb connected to the phone. Omit serial for a single
        adb device. Returns subscriptions with subscriptionId, zero-based slotIndex,
        carrierName and number. The number is null when Android has no number for a SIM;
        subscriptions is empty when no SIM is active. Preserves Android's number format.
        Runs independently of Codex, using a temporary app_process helper.
        """
        return await api.request(
            "GET",
            f"{_machine_path(machine_id)}/phone-numbers",
            params=_params(serial=serial, timeoutSeconds=timeout_seconds),
            timeout=timeout_seconds + 30,
        )

    @server.tool()
    async def list_files(
        machine_id: str,
        path: str = "~",
        search_term: str = "",
        mode: Literal["title", "content", "both"] = "title",
    ) -> dict[str, Any]:
        """List or search the direct children of a machine directory. Paths accept ~ and ~/.

        Search matches literal text, ignoring case. Title searches names; content searches readable
        text files. Both combines them. Searches stay within this directory, excluding descendants.
        """
        return await api.request(
            "GET",
            f"{_machine_path(machine_id)}/files",
            params={"path": path, "searchTerm": search_term, "mode": mode},
        )

    @server.tool()
    async def transfer_files(
        source_machine_id: str,
        source_paths: list[str],
        target_machine_id: str,
        target_directory: str,
        recursive: bool = False,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Copy files between registered machines, including the Nexus host's local machine.

        Both endpoints use IDs from list_machines and absolute filesystem paths. SSH transfers
        use scp through the Nexus host; remote machines need only be reachable from Nexus.
        Creates the target directory. Set recursive for directories. Existing names require
        overwrite=true; directory overwrites merge contents. The result contains destination paths.
        """
        return await api.request(
            "POST",
            "/api/files/transfer",
            json={
                "sourceMachineId": source_machine_id,
                "sourcePaths": source_paths,
                "targetMachineId": target_machine_id,
                "targetDirectory": target_directory,
                "recursive": recursive,
                "overwrite": overwrite,
            },
            timeout=600,
        )

    @server.tool()
    async def add_machine(
        name: str,
        transport: Literal["local", "ssh"] = "local",
        host: str | None = None,
        user: str | None = None,
        port: int = 22,
        codex_bin: str = "codex",
        projects: list[str] | None = None,
        connection: Literal["daemon", "spawn"] = "daemon",
    ) -> dict[str, Any]:
        """Register a local or SSH machine. Daemon mode reuses its shared Codex runtime."""
        return await api.request(
            "POST",
            "/api/machines",
            json=_params(
                name=name,
                transport=transport,
                host=host,
                user=user,
                port=port,
                codexBin=codex_bin,
                projects=projects,
                connection=connection,
            ),
        )

    @server.tool()
    async def update_machine(machine_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Update machine settings. Changed runtime settings disconnect the machine; metadata preserves connections.

        Use add_project or remove_project to change individual tracked directories.
        """
        return await api.request("PATCH", _machine_path(machine_id), json=settings)

    @server.tool()
    async def add_project(
        machine_id: str, cwd: str, create_directory: bool = False
    ) -> dict[str, Any]:
        """Track a project directory. Set create_directory to create missing directories and parents."""
        body = {"cwd": cwd}
        if create_directory:
            body["createDirectory"] = True
        return await api.request("POST", f"{_machine_path(machine_id)}/projects", json=body)

    @server.tool()
    async def remove_project(machine_id: str, cwd: str) -> dict[str, Any]:
        """Stop tracking a project, preserving the connection, files, history, and running turns."""
        return await api.request(
            "DELETE", f"{_machine_path(machine_id)}/projects", json={"cwd": cwd}
        )

    @server.tool()
    async def remove_machine(machine_id: str) -> dict[str, Any]:
        """Remove a machine from Nexus. Its shared Codex daemon and stored conversations remain."""
        return await api.request("DELETE", _machine_path(machine_id))

    @server.tool()
    async def connect_machine(machine_id: str) -> dict[str, Any]:
        """Connect Nexus to a registered machine's Codex runtime."""
        return await api.request("POST", f"{_machine_path(machine_id)}/connect")

    @server.tool()
    async def reconnect_machine(machine_id: str) -> dict[str, Any]:
        """Rebuild a machine connection immediately and restore observed sessions."""
        return await api.request("POST", f"{_machine_path(machine_id)}/reconnect")

    @server.tool()
    async def disconnect_machine(machine_id: str) -> dict[str, Any]:
        """Disconnect Nexus from a machine while leaving its shared Codex daemon running."""
        return await api.request("POST", f"{_machine_path(machine_id)}/disconnect")

    @server.tool()
    async def list_threads(
        machine_id: str | None = None,
        archive_status: ArchiveStatus = "all",
        scope: Literal["history", "loaded"] = "history",
        search_term: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List conversations in tracked projects.

        scope=history (default) includes saved, unloaded conversations; loaded selects current runtime sessions.
        archive_status selects all (default), unarchived, or archived history.
        History sorts by recent activity; limit applies per machine. The first page returns
        nextCursors keyed by machine ID. Continue with that machine_id and cursor; later pages return nextCursor.
        Loaded queries return all matching loaded sessions without pagination.
        """
        params = _params(
            scope=scope,
            searchTerm=search_term,
            limit=limit,
            archived=ARCHIVE_FILTERS[archive_status],
        )
        if cursor is not None and machine_id is not None:
            return await api.request(
                "GET", f"{_machine_path(machine_id)}/threads", params={**params, "cursor": cursor}
            )
        return await api.request(
            "GET", "/api/threads", params={**params, **_params(machineId=machine_id)}
        )

    @server.tool()
    async def search_threads(
        search_term: str,
        mode: Literal["title", "content", "both", "id"] = "title",
        view: Literal["recent", "working", "waiting"] = "recent",
        machine_id: str | None = None,
        cwd: str | None = None,
        archive_status: ArchiveStatus = "all",
        cursor: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search indexed conversations; archive_status selects all (default), unarchived, or archived.

        Modes: title, content, both, id (any ID suffix, ignores cwd). Views: recent, working, waiting.
        """
        return await api.request(
            "GET",
            "/api/search",
            params=_params(
                searchTerm=search_term,
                mode=mode,
                view=view,
                machineId=machine_id,
                cwd=cwd,
                archived=ARCHIVE_FILTERS[archive_status],
                cursor=cursor,
                limit=limit,
            ),
        )

    @server.tool()
    async def get_thread(machine_id: str, thread_id: str) -> dict[str, Any]:
        """Read a conversation's metadata and current runtime status."""
        return await api.request("GET", _thread_path(machine_id, thread_id))

    @server.tool()
    async def get_goal(machine_id: str, thread_id: str) -> dict[str, Any]:
        """Read the native goal, status, token budget, and accumulated usage."""
        return await api.request("GET", f"{_thread_path(machine_id, thread_id)}/goal")

    @server.tool()
    async def set_goal(machine_id: str, thread_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Set native objective, status, or tokenBudget (null clears the budget).

        Start or resume a goal only when the user requests it. Agent mutations use automatic origin.
        """
        return await api.request(
            "PUT", f"{_thread_path(machine_id, thread_id)}/goal",
            json={**settings, "origin": "automatic"},
        )

    @server.tool()
    async def resume_goal(machine_id: str, thread_id: str) -> dict[str, Any]:
        """Resume the native goal and its automatic continuation, on explicit user request."""
        return await api.request(
            "POST", f"{_thread_path(machine_id, thread_id)}/goal/resume",
            json={"origin": "automatic"},
        )

    @server.tool()
    async def stop_goal(machine_id: str, thread_id: str) -> dict[str, Any]:
        """Pause the goal and interrupt the current turn, retaining the objective and usage."""
        return await api.request(
            "POST", f"{_thread_path(machine_id, thread_id)}/goal/stop",
            json={"origin": "automatic"},
        )

    @server.tool()
    async def clear_goal(machine_id: str, thread_id: str) -> dict[str, Any]:
        """Remove a native goal on explicit user request; the current turn continues."""
        return await api.request(
            "DELETE", f"{_thread_path(machine_id, thread_id)}/goal",
            json={"origin": "automatic"},
        )

    @server.tool()
    async def get_thread_state(machine_id: str, thread_id: str) -> dict[str, Any]:
        """Read native model, reasoning effort, permissions, mode, and context token usage."""
        return await api.request("GET", f"{_thread_path(machine_id, thread_id)}/state")

    @server.tool()
    async def update_thread_settings(
        machine_id: str,
        thread_id: str,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        """Update native settings such as model, effort, serviceTier, or collaborationMode.

        Read model effort choices with list_models. Changes persist for subsequent turns;
        model, effort, and service tier also apply to subsequent steps of an active turn.
        """
        return await api.request(
            "PATCH",
            f"{_thread_path(machine_id, thread_id)}/settings",
            json=settings,
        )

    @server.tool()
    async def read_history(
        machine_id: str,
        thread_id: str,
        cursor: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Read full conversation turns, newest page first. Follow nextCursor for older history."""
        return await api.request(
            "GET",
            f"{_thread_path(machine_id, thread_id)}/history",
            params=_params(cursor=cursor, limit=limit),
        )

    @server.tool()
    async def create_thread(
        machine_id: str,
        cwd: str,
        model: str | None = None,
        sandbox: str | None = None,
        effort: str | None = None,
        approval_policy: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a Codex conversation in a directory. Omitted settings inherit shared Nexus and remote defaults."""
        return await api.request(
            "POST",
            f"{_machine_path(machine_id)}/threads",
            json=_params(
                cwd=cwd,
                model=model,
                sandbox=sandbox,
                effort=effort,
                approvalPolicy=approval_policy,
            ),
        )

    @server.tool()
    async def whoami(ctx: Context) -> dict[str, Any]:
        """Read your botId, machineId, and full threadId from the calling Codex session."""
        return await api.request(
            "GET", "/api/threads/resolve", params={"threadId": _caller_thread(ctx)}
        )

    @server.tool()
    async def resolve_thread(thread_id: str, machine_id: str | None = None) -> dict[str, Any]:
        """Resolve a six-character botId or full thread ID into botId, machineId, and threadId."""
        return await api.request(
            "GET", "/api/threads/resolve", params=_params(threadId=thread_id, machineId=machine_id)
        )

    @server.tool()
    async def list_groups() -> dict[str, Any]:
        """Read named groups and every member's botId, machineId, and full threadId."""
        return await api.request("GET", "/api/groups")

    @server.tool()
    async def set_group(
        name: str, thread_ids: Annotated[list[str], Field(min_length=1)]
    ) -> dict[str, Any]:
        """Create a group or replace its members using botIds or full thread IDs."""
        return await api.request(
            "PUT", f"/api/groups/{quote(name, safe='')}", json={"threadIds": thread_ids}
        )

    @server.tool()
    async def delete_group(name: str) -> dict[str, Any]:
        """Delete a named group, preserving its members and their conversations."""
        return await api.request("DELETE", f"/api/groups/{quote(name, safe='')}")

    @server.tool()
    async def send_message(
        text: str,
        ctx: Context,
        thread_id: str | list[str] | None = None,
        machine_id: str | None = None,
        group_name: str | None = None,
        cc: bool = False,
        input: list[dict[str, Any]] | None = None,
        actor_name: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
    ) -> dict[str, Any]:
        """Send to one botId/full thread ID, a list of recipients, or group_name.

        The caller's identity is automatic. Every recipient is awakened or steered.
        A recipient list sends independent messages; cc=true names all other recipients.
        Group messages name the group and go to its members except the sender.
        To post a reply to a group, call this tool with group_name.
        Input, when provided, replaces text and holds native text, image, or skill items.
        actor_name optionally sets the UI attribution label; the prompt uses your botId.
        Single delivery returns turnId/mode; lists and groups return data and errors per recipient.
        """
        return await api.request(
            "POST",
            "/api/messages",
            json=_params(
                senderThreadId=_caller_thread(ctx),
                threadId=thread_id,
                machineId=machine_id,
                groupName=group_name,
                cc=cc,
                text=text,
                input=input,
                actorName=actor_name,
                model=model,
                effort=effort,
                serviceTier=service_tier,
            ),
        )

    @server.tool()
    async def edit_message(
        machine_id: str,
        thread_id: str,
        turn_id: str,
        item_id: str,
        text: str,
        actor_name: str = "MCP",
    ) -> dict[str, Any]:
        """Replace a historical user message in the same conversation and regenerate from its turn.

        Stops the active turn and removes the edited turn and all later turns. Earlier user inputs
        in the same turn and the message's attachments are retained. Files keep their current state.
        """
        return await api.request(
            "POST",
            f"{_thread_path(machine_id, thread_id)}/messages/{quote(item_id, safe='')}/edit",
            json={"turnId": turn_id, "text": text, "actor": "agent", "actorName": actor_name},
        )

    @server.tool()
    async def regenerate_message(
        machine_id: str,
        thread_id: str,
        turn_id: str,
        item_id: str,
    ) -> dict[str, Any]:
        """Regenerate an assistant message in the same conversation.

        Retains all earlier messages, reasoning and tool records, including those in the same
        turn. Removes the selected message and later history, then continues with empty input.
        """
        return await api.request(
            "POST",
            f"{_thread_path(machine_id, thread_id)}/messages/{quote(item_id, safe='')}/regenerate",
            json={"turnId": turn_id},
        )

    @server.tool()
    async def regenerate_turn(
        machine_id: str,
        thread_id: str,
        turn_id: str,
    ) -> dict[str, Any]:
        """Regenerate a whole turn and replace later history in the same conversation.

        Resubmits every original user input and attachment in that turn. Turns without user
        input restart with empty input. Earlier turns and the working files are retained.
        """
        return await api.request(
            "POST",
            f"{_thread_path(machine_id, thread_id)}/turns/{quote(turn_id, safe='')}/regenerate",
            json={"actor": "agent", "actorName": "MCP"},
        )

    @server.tool()
    async def resume_turn(
        machine_id: str,
        thread_id: str,
        turn_id: str,
    ) -> dict[str, Any]:
        """Continue the latest interrupted turn using saved context and empty input.

        Starts a new turn in the same conversation without adding a user message.
        """
        return await api.request(
            "POST", f"{_thread_path(machine_id, thread_id)}/resume", json={"turnId": turn_id}
        )

    @server.tool()
    async def fork_thread(
        machine_id: str,
        thread_id: str,
        last_turn_id: str | None = None,
        before_turn_id: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
        turn_id: str | None = None,
        last_item_id: str | None = None,
    ) -> dict[str, Any]:
        """Create an independent conversation from history.

        last_turn_id includes that completed turn; before_turn_id excludes that turn.
        turn_id with last_item_id includes that assistant message and the preceding context.
        A partial final turn is interrupted and can be continued with resume_turn.
        Side conversations fork into sibling side conversations with the same main thread and side policy.
        Choose one boundary, or omit boundaries to fork the latest history. Files use the target directory's current state.
        """
        return await api.request(
            "POST",
            f"{_thread_path(machine_id, thread_id)}/fork",
            json=_params(
                lastTurnId=last_turn_id,
                beforeTurnId=before_turn_id,
                cwd=cwd,
                model=model,
                turnId=turn_id,
                lastItemId=last_item_id,
            ),
        )

    @server.tool()
    async def interrupt_turn(
        machine_id: str,
        thread_id: str,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        """Interrupt the current turn. Background terminals are controlled through their own tools."""
        return await api.request(
            "POST", f"{_thread_path(machine_id, thread_id)}/interrupt", json=_params(turnId=turn_id)
        )

    @server.tool()
    async def archive_thread(
        machine_id: str,
        thread_id: str,
        archived: bool = True,
    ) -> dict[str, Any]:
        """Archive a conversation, or restore it by passing archived=false."""
        action = "archive" if archived else "unarchive"
        return await api.request("POST", f"{_thread_path(machine_id, thread_id)}/{action}")

    @server.tool()
    async def rename_thread(machine_id: str, thread_id: str, name: str) -> dict[str, Any]:
        """Set a conversation's display name."""
        return await api.request(
            "POST", f"{_thread_path(machine_id, thread_id)}/rename", json={"name": name}
        )

    @server.tool()
    async def list_background_tasks(machine_id: str, thread_id: str) -> dict[str, Any]:
        """List the conversation's running background terminals, including Codex processId values."""
        return await api.request("GET", f"{_thread_path(machine_id, thread_id)}/background-tasks")

    @server.tool()
    async def terminate_background_task(
        machine_id: str,
        thread_id: str,
        process_id: str,
    ) -> dict[str, Any]:
        """Stop the selected background terminal using its Codex processId."""
        return await api.request(
            "POST",
            f"{_thread_path(machine_id, thread_id)}/background-tasks/{quote(process_id, safe='')}/terminate",
        )

    @server.tool()
    async def clean_background_tasks(machine_id: str, thread_id: str) -> dict[str, Any]:
        """Stop all background terminals owned by this conversation."""
        return await api.request(
            "POST", f"{_thread_path(machine_id, thread_id)}/background-tasks/clean"
        )

    @server.tool()
    async def list_requests(
        machine_id: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        """List pending Codex questions and approval requests. The returned id is used to respond."""
        return await api.request(
            "GET", "/api/requests", params=_params(machineId=machine_id, threadId=thread_id)
        )

    @server.tool()
    async def respond_request(request_id: str, result: dict[str, Any]) -> dict[str, Any]:
        """Reply to a pending request using its native Codex result, e.g. {"decision":"accept"}."""
        return await api.request(
            "POST", f"/api/requests/{quote(request_id, safe='')}/respond", json={"result": result}
        )

    @server.tool()
    async def list_models(machine_id: str) -> dict[str, Any]:
        """Read the model catalog supplied by this machine's Codex runtime."""
        return await api.request("GET", f"{_machine_path(machine_id)}/models")

    @server.tool()
    async def codex_rpc(
        machine_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Invoke a native Codex app-server method, preserving its method name, parameters, and result."""
        return await api.request(
            "POST",
            f"{_machine_path(machine_id)}/rpc",
            json={"method": method, "params": params or {}},
        )

    @server.tool()
    async def wait_for_thread(
        machine_id: str,
        thread_id: str,
        timeout_seconds: float = 60,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        """Wait for completion, an input request, or a runtime error.

        Returns reason, thread, and recent history or pending requests. On timeout, call again to continue waiting.
        Supply turn_id from send_message to wait for that exact turn.
        """
        path = _thread_path(machine_id, thread_id)
        return await api.request(
            "GET",
            f"{path}/wait",
            params=_params(timeoutSeconds=timeout_seconds, turnId=turn_id),
            timeout=max(60, timeout_seconds + 30),
        )

    return server
