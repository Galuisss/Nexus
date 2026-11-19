"""Explicit, interactive example network for trying Nexus without model calls."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path, PurePosixPath
from time import time
from uuid import uuid4

from .errors import NexusError


def timestamp() -> int:
    return int(time())


def message(text: str, role: str = "agent", item_id: str | None = None) -> dict:
    item_id = item_id or str(uuid4())
    if role == "user":
        return {"id": item_id, "type": "userMessage", "content": [{"type": "text", "text": text}]}
    return {"id": item_id, "type": "agentMessage", "text": text, "phase": "final_answer"}


class DemoNetwork:
    def __init__(self, emit, request, config_path: Path | None = None):
        self.emit = emit
        self.request = request
        config = json.loads((config_path or Path(__file__).with_name("demo.json")).read_text())
        self.machines = config["machines"]
        self.threads: dict[str, list[dict]] = {machine["id"]: [] for machine in self.machines}
        self.created_directories: dict[str, set[str]] = {}
        self.terminals: dict[tuple[str, str], list[dict]] = {}
        self.running: dict[tuple[str, str], asyncio.Task] = {}
        self.steers: dict[tuple[str, str], list[str]] = {}
        self.settings: dict[tuple[str, str], dict] = {}
        self.token_usage: dict[tuple[str, str], dict] = {}
        self.goals: dict[tuple[str, str], dict] = {}
        self.injected_items: dict[tuple[str, str], list[dict]] = {}
        self.seed_tasks: list[asyncio.Task] = []
        self.approvals: dict[tuple[str, str], dict] = {}
        self.seeded_threads = {
            spec["key"]: self._new_thread(
                spec["machineId"],
                spec["name"],
                spec["cwd"],
                spec.get("settings", {}).get("model", "gpt-6.1-sol"),
            )
            for spec in config["threads"]
        }
        self._seed(config)

    def _seed(self, config: dict) -> None:
        now = timestamp()
        turns = {}
        machines = {}
        for spec in config["threads"]:
            thread = self.seeded_threads[spec["key"]]
            machine_id = machines[spec["key"]] = spec["machineId"]
            key = (machine_id, thread["id"])
            settings = self.settings[key]
            settings.update(deepcopy(spec.get("settings", {})))
            settings["collaborationMode"]["settings"].update(
                {"model": settings["model"], "reasoning_effort": settings["effort"]}
            )
            thread.update(
                model=settings["model"],
                reasoningEffort=settings["effort"],
                status=deepcopy(spec.get("status", {"type": "idle"})),
                updatedAt=now - spec.get("age", 0),
            )
            for turn_spec in spec.get("turns", []):
                turn = self._seed_turn(turn_spec, now)
                thread["turns"].append(turn)
                turns[(spec["key"], turn_spec["key"])] = turn
            self.terminals[key] = [
                {
                    "cwd": thread["cwd"],
                    "osPid": None,
                    "cpuPercent": None,
                    "rssKb": None,
                    **deepcopy(terminal),
                }
                for terminal in spec.get("terminals", [])
            ]
            usage = spec.get("tokenUsage", config.get("tokenUsage"))
            if usage:
                self.token_usage[key] = deepcopy(usage)
            if goal := spec.get("goal"):
                self.goals[key] = {
                    "threadId": thread["id"],
                    "status": "active",
                    "tokenBudget": None,
                    "tokensUsed": 0,
                    "timeUsedSeconds": 0,
                    "createdAt": now,
                    "updatedAt": now,
                    **deepcopy(goal),
                }
        for request_spec in config.get("requests", []):
            request = deepcopy(request_spec)
            thread_key = request.pop("thread")
            thread = self.seeded_threads[thread_key]
            turn = turns[(thread_key, request.pop("turn"))]
            request["params"].update(threadId=thread["id"], turnId=turn["id"])
            machine_id = machines[thread_key]
            self.approvals[(machine_id, request["id"])] = request
            self.seed_tasks.append(asyncio.create_task(self.request(machine_id, deepcopy(request))))

    def _seed_turn(self, spec: dict, now: int) -> dict:
        turn = deepcopy(spec)
        turn.pop("key")
        age = turn.pop("age", 0)
        turn.setdefault("id", str(uuid4()))
        turn.setdefault("status", "completed")
        turn.setdefault("error", None)
        if turn["status"] == "inProgress":
            turn["startedAt"] = now - age
        else:
            duration = turn.setdefault("durationMs", 9000)
            turn["completedAt"] = now - age
            turn["startedAt"] = turn["completedAt"] - duration // 1000
        for item in turn["items"]:
            item.setdefault("id", str(uuid4()))
            delivery = item.get("origin", {}).get("delivery")
            if delivery and "senderThread" in delivery:
                sender = self.seeded_threads[delivery.pop("senderThread")]
                delivery["senderBotId"] = sender["id"][-6:]
        return turn

    def _new_thread(self, machine_id: str, name: str, cwd: str, model: str = "gpt-6.1-sol") -> dict:
        thread_id = str(uuid4())
        thread = {
            "id": thread_id,
            "sessionId": thread_id,
            "name": name,
            "preview": name,
            "cwd": cwd,
            "createdAt": timestamp(),
            "updatedAt": timestamp(),
            "turns": [],
            "status": {"type": "idle"},
            "source": "appServer",
            "threadSource": "nexus",
            "modelProvider": "openai",
            "model": model,
            "reasoningEffort": "high",
            "ephemeral": False,
            "historyMode": "paginated",
            "forkedFromId": None,
            "path": None,
        }
        self.threads.setdefault(machine_id, []).append(thread)
        self.settings[(machine_id, thread_id)] = {
            "model": model,
            "modelProvider": "openai",
            "effort": "high",
            "serviceTier": None,
            "summary": "auto",
            "cwd": cwd,
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
            "sandboxPolicy": {
                "type": "workspaceWrite",
                "writableRoots": [],
                "networkAccess": False,
                "excludeTmpdirEnvVar": False,
                "excludeSlashTmp": False,
            },
            "collaborationMode": {
                "mode": "default",
                "settings": {
                    "model": model,
                    "reasoning_effort": "high",
                    "developer_instructions": None,
                },
            },
        }
        return thread

    def _thread(self, machine_id: str, thread_id: str) -> dict:
        for thread in self.threads.get(machine_id, []):
            if thread["id"] == thread_id:
                return thread
        raise NexusError("找不到这个演示会话", code=-32602, status_code=404)

    def _summary(self, thread: dict, include_turns: bool = False) -> dict:
        result = deepcopy(thread)
        if not include_turns:
            result["turns"] = []
        return result

    def _session_result(self, machine_id: str, thread: dict, include_turns: bool = False) -> dict:
        settings = self.settings[(machine_id, thread["id"])]
        return {
            "thread": self._summary(thread, include_turns),
            "model": settings["model"],
            "summary": settings["summary"],
            "cwd": settings["cwd"],
            "reasoningEffort": settings["effort"],
            "serviceTier": settings["serviceTier"],
            "collaborationMode": deepcopy(settings["collaborationMode"]),
            "approvalPolicy": settings["approvalPolicy"],
            "sandbox": deepcopy(settings["sandboxPolicy"]),
        }

    async def _settings_update(self, machine_id: str, thread: dict, params: dict) -> None:
        settings = self.settings[(machine_id, thread["id"])]
        for field in (
            "model",
            "effort",
            "serviceTier",
            "collaborationMode",
            "approvalPolicy",
            "sandboxPolicy",
            "cwd",
            "summary",
        ):
            if field in params:
                settings[field] = deepcopy(params[field])
        if settings["serviceTier"] == "fast":
            settings["serviceTier"] = "priority"
        mode_settings = settings["collaborationMode"]["settings"]
        if "collaborationMode" in params:
            settings["model"] = mode_settings["model"]
            settings["effort"] = mode_settings.get("reasoning_effort")
        else:
            mode_settings["model"] = settings["model"]
            mode_settings["reasoning_effort"] = settings["effort"]
        thread["model"] = settings["model"]
        thread["reasoningEffort"] = settings["effort"]
        await self.emit(
            machine_id,
            "thread/settings/updated",
            {
                "threadId": thread["id"],
                "threadSettings": deepcopy(settings),
            },
        )

    @staticmethod
    def _page(data: list, params: dict) -> dict:
        offset, limit = int(params.get("cursor") or 0), params.get("limit") or 50
        return {
            "data": deepcopy(data[offset : offset + limit]),
            "nextCursor": str(offset + limit) if offset + limit < len(data) else None,
        }

    async def close(self) -> None:
        tasks = [*self.running.values(), *self.seed_tasks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def create_directory(self, machine_id: str, path: str) -> None:
        self.created_directories.setdefault(machine_id, set()).add(path)

    async def rpc(self, machine_id: str, method: str, params: dict) -> dict:
        if method == "account/read":
            return {
                "account": {"type": "chatgpt", "email": "alex@example.com", "planType": "pro"},
                "userId": "demo-user",
                "requiresOpenaiAuth": True,
                "workspaceRouting": {"chatgptAccountId": "demo-account"},
            }
        if method == "account/rateLimits/read":
            return {
                "accountId": "demo-account",
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {
                        "usedPercent": 28,
                        "windowDurationMins": 300,
                        "resetsAt": timestamp() + 7200,
                    },
                    "secondary": {
                        "usedPercent": 46,
                        "windowDurationMins": 10080,
                        "resetsAt": timestamp() + 259200,
                    },
                },
                "rateLimitResetCredits": {
                    "availableCount": 2,
                    "credits": [
                        {
                            "id": f"demo-reset-{index}",
                            "status": "available",
                            "expiresAt": timestamp() + 604800,
                        }
                        for index in range(2)
                    ],
                },
            }
        if method == "config/read":
            return {"config": {"model_reasoning_summary": "auto"}}
        if method == "thread/list":
            threads = [
                thread
                for thread in self.threads.get(machine_id, [])
                if bool(thread.get("archived")) == bool(params.get("archived"))
            ]
            if params.get("cwd"):
                paths = params["cwd"] if isinstance(params["cwd"], list) else [params["cwd"]]
                threads = [thread for thread in threads if thread["cwd"] in paths]
            search = params.get("searchTerm", "").lower()
            threads = [thread for thread in threads if search in (thread.get("name") or "").lower()]
            threads.sort(key=lambda thread: thread["updatedAt"], reverse=True)
            return self._page([self._summary(thread) for thread in threads], params)
        if method == "thread/loaded/list":
            return self._page(
                [
                    thread["id"]
                    for thread in self.threads.get(machine_id, [])
                    if thread["status"]["type"] != "notLoaded" and not thread.get("archived")
                ],
                params,
            )
        if method == "fs/readDirectory":
            machine = next(machine for machine in self.machines if machine["id"] == machine_id)
            paths = [
                *machine.get("projects", []),
                *self.created_directories.get(machine_id, set()),
                *(thread["cwd"] for thread in self.threads.get(machine_id, [])),
            ]
            directories = {PurePosixPath("/")}
            for path in filter(None, paths):
                directory = PurePosixPath(path)
                directories.update([directory, *directory.parents])
            current = PurePosixPath(params["path"])
            if current not in directories:
                raise NexusError("找不到这个目录", code="directory_not_found", status_code=404)
            return {
                "entries": [
                    {"fileName": path.name, "isDirectory": True, "isFile": False}
                    for path in directories
                    if path != current and path.parent == current
                ]
            }
        if method == "model/list":
            return {
                "data": [
                    {
                        "id": model,
                        "model": model,
                        "displayName": label,
                        "description": "演示模型",
                        "isDefault": model == "gpt-6.1-sol",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": effort, "description": effort} for effort in efforts
                        ],
                        "defaultReasoningEffort": "high",
                        "serviceTiers": [
                            {
                                "id": "priority",
                                "name": "Fast",
                                "description": "2x speed, increased usage",
                            }
                        ],
                        "defaultServiceTier": None,
                        "additionalSpeedTiers": ["fast"],
                    }
                    for model, label, efforts in [
                        (
                            "gpt-6.1-sol",
                            "GPT-6.1 Sol",
                            ["low", "medium", "high", "xhigh", "max", "ultra"],
                        ),
                        ("gpt-6.1-terra", "GPT-6.1 Terra", ["medium", "high", "xhigh", "ultra"]),
                    ]
                ],
                "nextCursor": None,
            }
        if method == "skills/list":
            return {
                "data": [
                    {
                        "cwd": cwd,
                        "errors": [],
                        "skills": [
                            {
                                "name": "code-review",
                                "description": "检查修改中的逻辑与实现问题。",
                                "enabled": True,
                                "path": cwd + "/.agents/skills/code-review/SKILL.md",
                                "scope": "repo",
                            },
                            {
                                "name": "implementation",
                                "description": "按项目约定完成代码实现。",
                                "enabled": True,
                                "path": cwd + "/.agents/skills/implementation/SKILL.md",
                                "scope": "repo",
                            },
                        ],
                    }
                    for cwd in params.get("cwds", [])
                ]
            }
        if method == "mcpServerStatus/list":
            return {
                "data": [
                    {
                        "name": "nexus",
                        "authStatus": "unsupported",
                        "tools": {},
                        "resources": [],
                        "resourceTemplates": [],
                        "serverInfo": {"name": "Nexus demo", "version": "0.1.0"},
                    }
                ],
                "nextCursor": None,
            }
        if method == "command/exec":
            command = params["command"]
            diff = (
                ""
                if "ls-files" in command
                else (
                    "diff --git a/src/nexus/core.py b/src/nexus/core.py\n"
                    "--- a/src/nexus/core.py\n+++ b/src/nexus/core.py\n"
                    "@@ -1,2 +1,3 @@\n"
                    " def observe_session(session):\n"
                    "-    return session.read()\n"
                    "+    session.resume()\n"
                    "+    return session.read()\n"
                )
            )
            return {"exitCode": 0, "stdout": diff, "stderr": ""}
        if method == "thread/start":
            machine = next(machine for machine in self.machines if machine["id"] == machine_id)
            config = params.get("config") or {}
            thread = self._new_thread(
                machine_id,
                "新会话",
                params.get("cwd") or next(iter(machine.get("projects", [])), "/workspace"),
                params.get("model") or config.get("model") or "gpt-6.1-sol",
            )
            settings = self.settings[(machine_id, thread["id"])]
            settings["effort"] = config.get("model_reasoning_effort", settings["effort"])
            settings["collaborationMode"]["settings"]["reasoning_effort"] = settings["effort"]
            tier = params.get("serviceTier", config.get("service_tier"))
            settings["serviceTier"] = "priority" if tier == "fast" else tier
            if params.get("approvalPolicy"):
                settings["approvalPolicy"] = params["approvalPolicy"]
            if params.get("sandbox"):
                settings["sandboxPolicy"] = params["sandbox"]
            await self.emit(machine_id, "thread/started", {"thread": self._summary(thread)})
            return self._session_result(machine_id, thread)

        thread_id = params.get("threadId")
        thread = self._thread(machine_id, thread_id)
        if method == "thread/read":
            return {"thread": self._summary(thread, params.get("includeTurns", False))}
        if method == "thread/goal/get":
            return {"goal": deepcopy(self.goals.get((machine_id, thread_id)))}
        if method == "thread/goal/set":
            key = (machine_id, thread_id)
            goal = self.goals.get(key)
            if goal is None:
                if not params.get("objective", "").strip():
                    raise NexusError("no goal exists", code=-32600)
                goal = self.goals[key] = {
                    "threadId": thread_id,
                    "objective": params["objective"].strip(),
                    "status": "active",
                    "tokenBudget": None,
                    "tokensUsed": 0,
                    "timeUsedSeconds": 0,
                    "createdAt": timestamp(),
                }
            goal.update({key: params[key] for key in ("objective", "status", "tokenBudget") if key in params})
            goal["updatedAt"] = timestamp()
            await self.emit(machine_id, "thread/goal/updated", {"threadId": thread_id, "goal": deepcopy(goal)})
            if goal["status"] == "active" and (machine_id, thread_id) not in self.running:
                await self.rpc(machine_id, "turn/start", {"threadId": thread_id, "input": []})
            return {"goal": deepcopy(goal)}
        if method == "thread/goal/clear":
            cleared = self.goals.pop((machine_id, thread_id), None) is not None
            if cleared:
                await self.emit(machine_id, "thread/goal/cleared", {"threadId": thread_id})
            return {"cleared": cleared}
        if method == "thread/delete":
            task = self.running.pop((machine_id, thread_id), None)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.threads[machine_id].remove(thread)
            self.settings.pop((machine_id, thread_id), None)
            self.token_usage.pop((machine_id, thread_id), None)
            self.goals.pop((machine_id, thread_id), None)
            self.injected_items.pop((machine_id, thread_id), None)
            self.steers.pop((machine_id, thread_id), None)
            await self.emit(machine_id, "thread/deleted", {"threadId": thread_id})
            return {}
        if method == "thread/inject_items":
            self.injected_items.setdefault((machine_id, thread_id), []).extend(
                deepcopy(params["items"])
            )
            return {}
        if method == "thread/resume":
            if thread["status"]["type"] == "notLoaded":
                thread["status"] = {"type": "idle"}
            await self.emit(machine_id, "thread/started", {"thread": self._summary(thread)})
            usage = self.token_usage.get((machine_id, thread_id))
            if usage and thread["turns"]:
                await self.emit(
                    machine_id,
                    "thread/tokenUsage/updated",
                    {
                        "threadId": thread_id,
                        "turnId": thread["turns"][-1]["id"],
                        "tokenUsage": deepcopy(usage),
                    },
                )
            result = self._session_result(machine_id, thread, not params.get("excludeTurns", False))
            if initial := params.get("initialTurnsPage"):
                turns = list(reversed(thread["turns"]))
                result["initialTurnsPage"] = self._page(turns, initial)
            return result
        if method == "thread/settings/update":
            await self._settings_update(machine_id, thread, params)
            return {}
        if method == "turn/settings/update":
            turn = next((turn for turn in thread["turns"] if turn["id"] == params["turnId"]), None)
            return {
                "status": "applied"
                if turn and turn["status"] == "inProgress"
                else "targetUnavailable"
            }
        if method == "thread/turns/list":
            turns = list(thread["turns"])
            if params.get("sortDirection", "desc") == "desc":
                turns.reverse()
            result = self._page(turns, params)
            if params.get("itemsView") == "notLoaded":
                for turn in result["data"]:
                    turn["items"] = []
            return result
        if method == "thread/compact/start":
            turn = {
                "id": str(uuid4()),
                "status": "inProgress",
                "startedAt": timestamp(),
                "items": [{"type": "contextCompaction", "id": str(uuid4())}],
                "error": None,
            }
            thread["turns"].append(turn)
            await self.emit(
                machine_id, "turn/started", {"threadId": thread_id, "turn": deepcopy(turn)}
            )
            await self.emit(
                machine_id,
                "item/completed",
                {"threadId": thread_id, "turnId": turn["id"], "item": turn["items"][0]},
            )
            await self._finish_turn(machine_id, thread, turn, "completed")
            usage = self.token_usage[(machine_id, thread_id)]
            usage["last"].update(
                {
                    "inputTokens": 7600,
                    "outputTokens": 180,
                    "totalTokens": 7780,
                    "cachedInputTokens": 6800,
                    "reasoningOutputTokens": 60,
                }
            )
            await self.emit(
                machine_id,
                "thread/tokenUsage/updated",
                {
                    "threadId": thread_id,
                    "turnId": turn["id"],
                    "tokenUsage": deepcopy(usage),
                },
            )
            return {}
        if method == "review/start":
            result = await self.rpc(
                machine_id,
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": "审查当前未提交的修改。"}],
                },
            )
            return {"reviewThreadId": thread_id, **result}
        if method == "thread/items/list":
            items = [
                {"turnId": turn["id"], "item": item}
                for turn in thread["turns"]
                if not params.get("turnId") or turn["id"] == params["turnId"]
                for item in turn["items"]
            ]
            if params.get("sortDirection") == "desc":
                items.reverse()
            return self._page(items, params)
        if method == "thread/fork":
            turns = thread["turns"]
            if params.get("lastTurnId"):
                index = next(
                    (
                        index
                        for index, turn in enumerate(turns)
                        if turn["id"] == params["lastTurnId"]
                    ),
                    None,
                )
                if index is None or turns[index]["status"] == "inProgress":
                    raise NexusError("请选择已经结束的轮次", code=-32602)
                turns = turns[: index + 1]
            elif params.get("beforeTurnId"):
                index = next(
                    (
                        index
                        for index, turn in enumerate(turns)
                        if turn["id"] == params["beforeTurnId"]
                    ),
                    None,
                )
                if index is None:
                    raise NexusError("找不到这个轮次", code=-32602)
                turns = turns[:index]
            fork = self._new_thread(
                machine_id,
                thread.get("name"),
                params.get("cwd") or thread["cwd"],
                params.get("model") or thread["model"],
            )
            fork["turns"] = deepcopy(turns)
            for turn in fork["turns"]:
                if turn["status"] == "inProgress":
                    turn["status"] = "interrupted"
                    turn["completedAt"] = timestamp()
            fork["forkedFromId"] = thread_id
            fork["threadSource"] = params.get("threadSource", "nexus")
            if params.get("attachToSource"):
                fork["attachedToThreadId"] = thread_id
                fork["turns"] = []
            fork["sessionId"] = thread["sessionId"]
            self.settings[(machine_id, fork["id"])] = deepcopy(
                self.settings[(machine_id, thread_id)]
            )
            if "model" in params:
                self.settings[(machine_id, fork["id"])]["model"] = params["model"]
            await self.emit(machine_id, "thread/started", {"thread": self._summary(fork)})
            return self._session_result(machine_id, fork, not params.get("excludeTurns", False))
        if method == "thread/revert":
            if any(turn["status"] == "inProgress" for turn in thread["turns"]):
                raise NexusError("请先停止当前轮", code=-32600)
            index = next(
                (
                    i
                    for i, turn in enumerate(thread["turns"])
                    if turn["id"] == params["beforeTurnId"]
                ),
                None,
            )
            if index is None:
                raise NexusError("找不到这个轮次", code=-32602)
            thread["turns"] = thread["turns"][:index]
            thread["status"] = {"type": "idle"}
            thread["updatedAt"] = timestamp()
            self.steers.pop((machine_id, thread_id), None)
            self.token_usage.pop((machine_id, thread_id), None)
            await self.emit(machine_id, "thread/reverted", {"threadId": thread_id})
            return {"thread": self._summary(thread)}
        if method == "thread/name/set":
            thread["name"] = params["name"]
            await self.emit(
                machine_id,
                "thread/name/updated",
                {"threadId": thread_id, "threadName": thread["name"]},
            )
            return {}
        if method in {"thread/archive", "thread/unarchive"}:
            thread["archived"] = method == "thread/archive"
            await self.emit(
                machine_id,
                "thread/archived" if thread["archived"] else "thread/unarchived",
                {"threadId": thread_id},
            )
            return {}
        if method == "turn/start":
            if thread["turns"] and thread["turns"][-1]["status"] == "inProgress":
                return await self.rpc(
                    machine_id,
                    "turn/steer",
                    {**params, "expectedTurnId": thread["turns"][-1]["id"]},
                )
            await self._settings_update(machine_id, thread, params)
            turn = {
                "id": str(uuid4()),
                "status": "inProgress",
                "items": [],
                "startedAt": timestamp(),
                "error": None,
            }
            thread["turns"].append(turn)
            thread["status"] = {"type": "active", "activeFlags": []}
            thread["updatedAt"] = timestamp()
            text = await self._append_input(machine_id, thread, turn, params)
            if thread["name"] == "新会话":
                thread["name"] = text[:32] or "新会话"
                thread["preview"] = text[:200]
            await self.emit(
                machine_id, "turn/started", {"threadId": thread_id, "turn": deepcopy(turn)}
            )
            task = asyncio.create_task(self._run_turn(machine_id, thread, turn, text))
            self.running[(machine_id, thread_id)] = task
            return {"turn": deepcopy(turn)}
        if method == "turn/steer":
            if not thread["turns"] or thread["turns"][-1]["status"] != "inProgress":
                raise NexusError("no active turn to steer", code=-32600)
            turn = thread["turns"][-1]
            if params["expectedTurnId"] != turn["id"]:
                raise NexusError(
                    f"expected active turn id `{params['expectedTurnId']}` but found `{turn['id']}`",
                    code=-32600,
                )
            text = await self._append_input(machine_id, thread, turn, params)
            self.steers.setdefault((machine_id, thread_id), []).append(text)
            return {"turnId": turn["id"]}
        if method == "turn/interrupt":
            task = self.running.pop((machine_id, thread_id), None)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if thread["turns"] and thread["turns"][-1]["status"] == "inProgress":
                await self._finish_turn(machine_id, thread, thread["turns"][-1], "interrupted")
            self.steers.pop((machine_id, thread_id), None)
            for key, request in list(self.approvals.items()):
                pending = request["params"]
                if key[0] == machine_id and (pending["threadId"], pending["turnId"]) == (
                    thread_id,
                    params["turnId"],
                ):
                    self.approvals.pop(key)
                    await self.emit(
                        machine_id,
                        "serverRequest/resolved",
                        {"threadId": thread_id, "requestId": request["id"]},
                    )
            return {}
        if method == "thread/backgroundTerminals/list":
            return self._page(self.terminals.get((machine_id, thread_id), []), params)
        if method == "thread/backgroundTerminals/terminate":
            tasks = self.terminals.get((machine_id, thread_id), [])
            match = next((task for task in tasks if task["processId"] == params["processId"]), None)
            if match:
                tasks.remove(match)
                await self.emit(
                    machine_id, "nexus/background-tasks/updated", {"threadId": thread_id}
                )
            return {"terminated": bool(match)}
        if method == "thread/backgroundTerminals/clean":
            self.terminals[(machine_id, thread_id)] = []
            await self.emit(machine_id, "nexus/background-tasks/updated", {"threadId": thread_id})
            return {}
        raise NexusError(f"演示模式未实现 {method}", code=-32601)

    async def _append_input(self, machine_id: str, thread: dict, turn: dict, params: dict) -> str:
        if not params.get("input"):
            return ""
        text = "\n".join(
            item.get("text", "") for item in params.get("input", []) if item.get("type") == "text"
        )
        item = message(text, "user", params.get("clientUserMessageId"))
        item["content"] = deepcopy(params.get("input", []))
        turn["items"].append(item)
        await self.emit(
            machine_id,
            "item/started",
            {"threadId": thread["id"], "turnId": turn["id"], "item": deepcopy(item)},
        )
        await self.emit(
            machine_id,
            "item/completed",
            {"threadId": thread["id"], "turnId": turn["id"], "item": deepcopy(item)},
        )
        return text

    async def _run_turn(self, machine_id: str, thread: dict, turn: dict, text: str) -> None:
        item = message("")
        turn["items"].append(item)
        base = f"收到：{text}\n\n这是 Nexus 的交互演示。当前消息已送入这个会话，你可以在回复过程中继续补充指令，或者中断当前轮。\n\n会话历史、分叉和后台任务控制都可以通过界面、HTTP API 或 MCP 操作。"
        if text == "审查当前未提交的修改。":
            base = "演示审查已完成。\n\n本次修改让界面在打开会话时显式订阅原生事件。原有的会话执行与 steering 行为保持一致。\n\n未发现需要修正的问题。"
        params = {"threadId": thread["id"], "turnId": turn["id"], "itemId": item["id"]}
        await self.emit(
            machine_id,
            "item/started",
            {**params, "item": deepcopy(item), "startedAtMs": int(time() * 1000)},
        )
        for offset in range(0, len(base), 7):
            await asyncio.sleep(0.08)
            delta = base[offset : offset + 7]
            item["text"] += delta
            await self.emit(machine_id, "item/agentMessage/delta", {**params, "delta": delta})
        for steer in self.steers.pop((machine_id, thread["id"]), []):
            delta = f"\n\n已收到本轮补充指令：{steer}"
            item["text"] += delta
            await self.emit(machine_id, "item/agentMessage/delta", {**params, "delta": delta})
        await self.emit(
            machine_id,
            "item/completed",
            {
                **params,
                "item": deepcopy(item),
                "completedAtMs": int(time() * 1000),
                "contentTokenCount": round(len(item["text"].encode()) / 4),
            },
        )
        await self._finish_turn(machine_id, thread, turn, "completed")
        self.running.pop((machine_id, thread["id"]), None)

    async def _finish_turn(self, machine_id: str, thread: dict, turn: dict, status: str) -> None:
        turn["status"] = status
        turn["completedAt"] = timestamp()
        turn["durationMs"] = (timestamp() - turn["startedAt"]) * 1000
        thread["status"] = {"type": "idle"}
        thread["updatedAt"] = timestamp()
        previous = self.token_usage.get((machine_id, thread["id"]))
        total = (
            deepcopy(previous["total"])
            if previous
            else {
                "totalTokens": 0,
                "inputTokens": 0,
                "outputTokens": 0,
                "cachedInputTokens": 0,
                "reasoningOutputTokens": 0,
                "cacheWriteInputTokens": 0,
            }
        )
        last = {
            "totalTokens": 13820,
            "inputTokens": 13000,
            "outputTokens": 820,
            "cachedInputTokens": 11200,
            "reasoningOutputTokens": 400,
            "cacheWriteInputTokens": 0,
        }
        for field, value in last.items():
            total[field] += value
        usage = {"last": last, "total": total, "modelContextWindow": 258400}
        self.token_usage[(machine_id, thread["id"])] = usage
        await self.emit(
            machine_id,
            "thread/tokenUsage/updated",
            {
                "threadId": thread["id"],
                "turnId": turn["id"],
                "tokenUsage": deepcopy(usage),
            },
        )
        summary = deepcopy(turn)
        summary["items"] = [item for item in summary["items"] if item["type"] == "agentMessage"][
            -1:
        ]
        summary["itemsView"] = "summary"
        await self.emit(machine_id, "turn/completed", {"threadId": thread["id"], "turn": summary})

    async def respond(self, machine_id: str, request_id: str, result: dict) -> None:
        request = self.approvals.pop((machine_id, request_id), None)
        if request is None:
            return
        params = request["params"]
        thread_id, turn_id = params["threadId"], params["turnId"]
        thread = self._thread(machine_id, thread_id)
        turn = next(turn for turn in thread["turns"] if turn["id"] == turn_id)
        item = next(item for item in turn["items"] if item["id"] == params["itemId"])
        accepted = result.get("decision") in {"accept", "acceptForSession"}
        item.update(
            {
                "status": "completed" if accepted else "declined",
                "exitCode": 0 if accepted else None,
                "aggregatedOutput": "演示：6 passed in 0.42s\n" if accepted else "命令已取消。",
            }
        )
        await self.emit(
            machine_id,
            "item/completed",
            {"threadId": thread_id, "turnId": turn_id, "item": deepcopy(item)},
        )
        answer = message("演示测试已完成。" if accepted else "已按你的选择停止这次命令。")
        turn["items"].append(answer)
        await self.emit(
            machine_id,
            "item/completed",
            {"threadId": thread_id, "turnId": turn_id, "item": deepcopy(answer)},
        )
        await self._finish_turn(machine_id, thread, turn, "completed")
        await self.emit(
            machine_id,
            "serverRequest/resolved",
            {"threadId": thread_id, "requestId": request_id},
        )
