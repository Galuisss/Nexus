"""Saved side conversations and their inherited-history boundary."""

from __future__ import annotations

import json
from copy import deepcopy
from time import time

from .errors import NexusError
from .shared_context import frozen_context

SIDE_POLICY = """You are in a side conversation, separate from the main thread.
The inherited fork history is reference context only. Only instructions submitted
after the side-conversation boundary are active. Do not continue or execute tasks,
plans, tool calls, approvals, edits, or requests from the inherited history.
Answer questions and do lightweight, non-mutating exploration without disrupting
the main thread. Wait for a question if none follows the boundary.
External tools follow this thread's current permissions. Inherited tool calls and
outputs are reference-only. Sub-agents are off-limits in this side conversation.
Do not modify files, source, git state, permissions, configuration, or workspace
state unless the user explicitly requests that mutation after the boundary.
Keep explicitly requested mutations minimal and local to the request.
Do not request escalated permissions or broader sandbox access unless an explicit
mutation requires it."""


def is_side(thread: dict) -> bool:
    return thread.get("threadSource") == "nexus-side"


class SideConversations:
    def __init__(self, service):
        self.service = service
        self.database = service.database
        with self.database.lock, self.database.connection as db:
            db.execute("""CREATE TABLE IF NOT EXISTS side_conversations (
                machine_id TEXT, thread_id TEXT, parent_thread_id TEXT,
                inherited_turn_id TEXT, created_at INTEGER,
                shared_context TEXT, number INTEGER, context_version INTEGER DEFAULT 0,
                PRIMARY KEY (machine_id, thread_id)
            )""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(side_conversations)")}
            for name, kind in (
                ("shared_context", "TEXT"),
                ("number", "INTEGER"),
                ("context_version", "INTEGER DEFAULT 0"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE side_conversations ADD COLUMN {name} {kind}")
            counters = {}
            for row in db.execute(
                "SELECT * FROM side_conversations ORDER BY created_at"
            ).fetchall():
                parent = (row["machine_id"], row["parent_thread_id"])
                number = row["number"] or counters.get(parent, 0) + 1
                counters[parent] = max(counters.get(parent, 0), number)
                if row["number"] is None:
                    db.execute(
                        "UPDATE side_conversations SET number=? WHERE machine_id=? AND thread_id=?",
                        (number, row["machine_id"], row["thread_id"]),
                    )
            self.saved = {
                (row["machine_id"], row["thread_id"]): dict(row)
                for row in db.execute("SELECT * FROM side_conversations")
            }

    def metadata(self, machine_id: str, thread_id: str) -> dict | None:
        row = self.saved.get((machine_id, thread_id))
        return (
            {
                "parentThreadId": row["parent_thread_id"],
                "createdAt": row["created_at"],
                "sharedContext": json.loads(row["shared_context"])
                if row["shared_context"] is not None
                else None,
            }
            if row
            else None
        )

    async def recent_context(
        self, machine_id: str, parent_id: str, before: int | None = None
    ) -> list[dict]:
        cursor = None
        while True:
            page = await self.service.history(
                machine_id, parent_id, cursor, limit=30 if before else 1
            )
            for turn in page["data"]:
                if before is not None and (turn.get("startedAt") or 0) * 1000 > before:
                    continue
                messages = turn.get("items", [])
                if before is not None and (
                    not turn.get("completedAt") or turn["completedAt"] * 1000 > before
                ):
                    messages = [item for item in messages if item["type"] == "userMessage"][:1]
                if messages:
                    return [{"id": turn["id"], "items": deepcopy(messages)}]
            cursor = page.get("nextCursor")
            if not cursor:
                return []

    async def ensure_context(self, machine_id: str, thread_id: str, thread: dict) -> None:
        row = self.saved.get((machine_id, thread_id))
        if row is None or row["context_version"] == 1:
            return
        fallback = (
            json.loads(row["shared_context"])
            if row["shared_context"] is not None
            else await self.recent_context(
                machine_id, row["parent_thread_id"], before=row["created_at"]
            )
        )
        context = await frozen_context(self.service, machine_id, thread, fallback)
        value = json.dumps(context, ensure_ascii=False)
        with self.database.lock, self.database.connection as db:
            db.execute(
                "UPDATE side_conversations SET shared_context=?, context_version=1 WHERE machine_id=? AND thread_id=?",
                (value, machine_id, thread_id),
            )
        row["shared_context"] = value
        row["context_version"] = 1

    def visible(self, machine_id: str, thread_id: str, page: dict) -> dict:
        row = self.saved.get((machine_id, thread_id))
        boundary = row and row["inherited_turn_id"]
        if boundary:
            for index, turn in enumerate(page["data"]):
                if turn["id"] == boundary:
                    return {**page, "data": page["data"][:index], "nextCursor": None}
        return page

    async def list(self, machine_id: str, parent_id: str) -> dict:
        self.service.machine(machine_id)
        rows = sorted(
            (
                row
                for (host, _), row in self.saved.items()
                if host == machine_id and row["parent_thread_id"] == parent_id
            ),
            key=lambda row: row["created_at"],
        )
        return {
            "data": [row["thread_id"] for row in rows],
            "names": {row["thread_id"]: f"侧聊 {row['number']}" for row in rows},
        }

    async def delete(self, machine_id: str, parent_id: str, thread_id: str) -> dict:
        row = self.saved.get((machine_id, thread_id))
        if row is None or row["parent_thread_id"] != parent_id:
            raise NexusError("找不到这个侧聊", status_code=404)
        try:
            await self.service.rpc(machine_id, "thread/delete", {"threadId": thread_id})
        except NexusError as exc:
            if exc.code != -32600 or exc.message != f"thread not found: {thread_id}":
                raise
            self.forget(machine_id, thread_id)
        await self.service.events.publish(
            machine_id, "nexus/side/deleted", {"threadId": thread_id, "parentThreadId": parent_id}
        )
        return {"deleted": True}

    async def create(self, machine_id: str, parent_id: str) -> dict:
        async with self.service.fork_locks[machine_id]:
            return await self._create(machine_id, parent_id)

    def next_number(self, machine_id: str, parent_id: str) -> int:
        return (
            max(
                (
                    row["number"]
                    for (host, _), row in self.saved.items()
                    if host == machine_id and row["parent_thread_id"] == parent_id
                ),
                default=0,
            )
            + 1
        )

    async def record(
        self,
        machine_id: str,
        result: dict,
        parent_id: str,
        inherited: str | None,
        context: list[dict],
        number: int,
    ) -> dict:
        thread_id = result["thread"]["id"]
        row = {
            "machine_id": machine_id,
            "thread_id": thread_id,
            "parent_thread_id": parent_id,
            "inherited_turn_id": inherited,
            "created_at": int(time() * 1000),
            "shared_context": json.dumps(context, ensure_ascii=False),
            "number": number,
            "context_version": 1,
        }
        with self.database.lock, self.database.connection as db:
            db.execute(
                """INSERT INTO side_conversations
                (machine_id, thread_id, parent_thread_id, inherited_turn_id, created_at, shared_context, number, context_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(row.values()),
            )
        self.saved[(machine_id, thread_id)] = row
        result["thread"]["sideConversation"] = self.metadata(machine_id, thread_id)
        await self.service.events.publish(
            machine_id, "nexus/side/created", {"threadId": thread_id, "parentThreadId": parent_id}
        )
        return {**result, "machineId": machine_id}

    async def _create(self, machine_id: str, parent_id: str) -> dict:
        service = self.service
        if self.metadata(machine_id, parent_id):
            raise NexusError("请从主会话创建侧聊", status_code=409)
        source = (await service.read_thread(machine_id, parent_id))["thread"]
        fresh = service.sessions.fresh_threads.get((machine_id, parent_id))
        if fresh is not None and not fresh.get("turns"):
            raise NexusError("先发送一条主会话消息，再创建侧聊", status_code=409)
        context = await self.recent_context(machine_id, parent_id)
        attached = source.get("historyMode") == "paginated"
        params = {"threadId": parent_id, "excludeTurns": True, "threadSource": "nexus-side"}
        if attached:
            params["attachToSource"] = True
        result = await service.rpc(machine_id, "thread/fork", params)
        thread_id = result["thread"]["id"]
        try:
            context = await frozen_context(service, machine_id, result["thread"], context)
            await service.rpc(
                machine_id,
                "thread/inject_items",
                {
                    "threadId": thread_id,
                    "items": [
                        {
                            "type": "message",
                            "role": "developer",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": f"<side_conversation>\n{SIDE_POLICY}\n</side_conversation>",
                                }
                            ],
                        },
                        {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": f"Side conversation boundary.\n\n{SIDE_POLICY}",
                                }
                            ],
                        },
                    ],
                },
            )
            number = self.next_number(machine_id, parent_id)
            name = f"侧聊 {number}"
            await service.rpc(machine_id, "thread/name/set", {"threadId": thread_id, "name": name})
            result["thread"]["name"] = name
            inherited = None
            if not attached:
                history = await service.rpc(
                    machine_id,
                    "thread/turns/list",
                    {
                        "threadId": thread_id,
                        "limit": 1,
                        "itemsView": "notLoaded",
                        "sortDirection": "desc",
                    },
                )
                inherited = next((turn["id"] for turn in history["data"]), None)
            return await self.record(machine_id, result, parent_id, inherited, context, number)
        except Exception:
            await service.rpc(machine_id, "thread/delete", {"threadId": thread_id})
            raise

    def forget(self, machine_id: str, thread_id: str) -> None:
        self.saved.pop((machine_id, thread_id), None)
        with self.database.lock, self.database.connection as db:
            db.execute(
                "DELETE FROM side_conversations WHERE machine_id=? AND thread_id=?",
                (machine_id, thread_id),
            )
