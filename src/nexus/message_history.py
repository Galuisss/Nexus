"""Rebuild a partial turn in Codex's replacement rollout."""

import asyncio
import base64
import io
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

import zstandard

from .errors import NexusError


@dataclass
class MessagePrefix:
    turn: dict
    records: list[dict]


class MessageHistory:
    def __init__(self, service):
        self.service = service
        self.rewriting: dict[tuple[str, str], asyncio.Event] = {}

    async def ready(self, machine_id: str, thread_id: str) -> None:
        if event := self.rewriting.get((machine_id, thread_id)):
            await event.wait()

    @asynccontextmanager
    async def rewrite(self, machine_id: str, thread_id: str):
        key = (machine_id, thread_id)
        event = self.rewriting[key] = asyncio.Event()
        try:
            yield
        finally:
            del self.rewriting[key]
            event.set()
            await self.service._notification(machine_id, "thread/reverted", {"threadId": thread_id})

    async def read(self, machine_id: str, path: str, end: int | None = None) -> tuple[str, bytes]:
        try:
            result = await self.service.rpc(machine_id, "fs/readFile", {"path": path})
        except NexusError as exc:
            if "(os error 2)" not in exc.message or path.endswith(".zst"):
                raise
            path += ".zst"
            result = await self.service.rpc(machine_id, "fs/readFile", {"path": path})
        data = base64.b64decode(result["dataBase64"])
        if path.endswith(".zst"):
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)) as reader:
                data = reader.read()
        return path, data[:end]

    async def ancestor_path(self, machine_id: str, path: str, rollout_id: str) -> str:
        root = next(
            parent.parent
            for parent in PurePosixPath(path).parents
            if parent.name in {"sessions", "archived_sessions"}
        )
        result = await self.service.commands.execute(
            machine_id,
            [
                "find",
                str(root / "sessions"),
                str(root / "archived_sessions"),
                "-type",
                "f",
                "(",
                "-name",
                f"*{rollout_id}.jsonl",
                "-o",
                "-name",
                f"*{rollout_id}.jsonl.zst",
                ")",
                "-print",
                "-quit",
            ],
            str(root),
        )
        if found := result["stdout"].strip():
            return found
        raise NexusError("找不到分叉前的原始历史文件", status_code=409)

    async def prefix(
        self, machine_id: str, thread_id: str, turn_id: str, item_id: str, *, inclusive: bool
    ) -> MessagePrefix:
        cursor = None
        while True:
            page = await self.service.rpc(
                machine_id,
                "thread/turns/list",
                {
                    "threadId": thread_id,
                    "limit": 30,
                    "cursor": cursor,
                    "itemsView": "full",
                    "sortDirection": "desc",
                },
            )
            turn = next((turn for turn in page["data"] if turn["id"] == turn_id), None)
            if turn:
                break
            cursor = page.get("nextCursor")
            if not cursor:
                raise NexusError("找不到这个轮次，请刷新会话", status_code=404)
        index = next(
            (
                i
                for i, item in enumerate(turn["items"])
                if item["id"] == item_id and item["type"] == "agentMessage"
            ),
            None,
        )
        if index is None:
            raise NexusError("找不到这条模型消息，请刷新会话", status_code=404)
        turn = deepcopy(turn)
        selected = turn["items"][index]
        complete = inclusive and turn["status"] == "completed" and index == len(turn["items"]) - 1
        turn["items"] = turn["items"][: index + int(inclusive)]
        turn.update(status="completed" if complete else "interrupted", error=None)
        if self.service.demo_network:
            return MessagePrefix(turn, [])

        thread = (await self.service.read_thread(machine_id, thread_id))["thread"]
        path, end, suffix = thread["path"], None, []
        while True:
            path, data = await self.read(machine_id, path, end)
            records = [json.loads(line) for line in data.splitlines()]
            records.extend(suffix)
            start = next(
                (
                    i
                    for i, record in enumerate(records)
                    if record["type"] == "event_msg"
                    and record["payload"].get("type") == "task_started"
                    and record["payload"]["turn_id"] == turn_id
                ),
                None,
            )
            if start is not None:
                records = records[start:]
                break
            boundary = records[0]["payload"].get("history_base")
            if not boundary:
                raise NexusError("找不到消息对应的原始轮次", status_code=409)
            suffix = records[1:]
            path = await self.ancestor_path(machine_id, path, boundary["thread_id"])
            end = boundary["end_byte_offset"]

        raw_index = next(
            (
                i
                for i, record in enumerate(records)
                if record["type"] == "response_item"
                and record["payload"].get("type") == "message"
                and record["payload"].get("role") == "assistant"
                and record["payload"].get("id") == item_id
            ),
            None,
        )
        if raw_index is None:
            raise NexusError("这条消息仍在保存中，请稍后重试", status_code=409)
        if inclusive:
            records = records[: raw_index + 1]
        else:
            completed = next(
                (
                    i
                    for i, record in enumerate(records[:raw_index])
                    if record["type"] == "event_msg"
                    and record["payload"].get("type") == "item_completed"
                    and record["payload"].get("item", {}).get("id") == item_id
                ),
                raw_index,
            )
            records = records[:completed]

        if inclusive:
            finished = selected.get("completedAtMs")
        elif turn["items"]:
            finished = turn["items"][-1].get("completedAtMs")
        else:
            finished = None
        completed_at = (
            finished / 1000
            if finished is not None
            else datetime.fromisoformat(records[-1]["timestamp"]).timestamp()
        )
        started = records[0]["payload"]
        started_at = (
            started.get("started_at") or datetime.fromisoformat(records[0]["timestamp"]).timestamp()
        )
        records.append(
            {
                "timestamp": datetime.fromtimestamp(completed_at).astimezone().isoformat(),
                "type": "event_msg",
                "payload": {
                    "type": "task_complete" if complete else "turn_aborted",
                    "turn_id": turn_id,
                    "root_turn_id": started.get("root_turn_id", turn_id),
                    "started_at": int(started_at),
                    "completed_at": int(completed_at),
                    "duration_ms": max(0, int((completed_at - started_at) * 1000)),
                    **(
                        {"last_agent_message": selected.get("text")}
                        if complete
                        else {"reason": "interrupted"}
                    ),
                },
            }
        )
        return MessagePrefix(turn, records)

    async def append(
        self, machine_id: str, thread_id: str, prefix: MessagePrefix, settings: dict
    ) -> None:
        if self.service.demo_network:
            self.service.demo_network._thread(machine_id, thread_id)["turns"].append(
                deepcopy(prefix.turn)
            )
            return

        # Archive drains the native recorder. Extend only the new fork/revert rollout;
        # its immutable ancestors and Codex's SQLite history remain owned by Codex.
        await self.service.rpc(machine_id, "thread/archive", {"threadId": thread_id})
        try:
            thread = (await self.service.rpc(machine_id, "thread/read", {"threadId": thread_id}))[
                "thread"
            ]
            path, data = await self.read(machine_id, thread["path"])
            ordinal = json.loads(data.splitlines()[-1])["ordinal"] + 1
            records = deepcopy(prefix.records)
            for index, record in enumerate(records):
                record["ordinal"] = ordinal + index
                if "thread_id" in record["payload"]:
                    record["payload"]["thread_id"] = thread_id
            data += "".join(
                json.dumps(record, ensure_ascii=False) + "\n" for record in records
            ).encode()
            if path.endswith(".zst"):
                data = zstandard.ZstdCompressor().compress(data)
            await self.service.rpc(
                machine_id,
                "fs/writeFile",
                {
                    "path": path,
                    "dataBase64": base64.b64encode(data).decode(),
                },
            )
        finally:
            await self.service.rpc(machine_id, "thread/unarchive", {"threadId": thread_id})
        await self.service.rpc(
            machine_id, "thread/resume", {"threadId": thread_id, "excludeTurns": True}
        )
        params = {
            field: value
            for field, value in settings.items()
            if field
            in {
                "model",
                "serviceTier",
                "approvalPolicy",
                "approvalsReviewer",
                "collaborationMode",
                "cwd",
                "summary",
            }
        }
        if "reasoningEffort" in settings:
            params["effort"] = settings["reasoningEffort"]
        if "sandbox" in settings:
            params["sandboxPolicy"] = settings["sandbox"]
        await self.service.rpc(
            machine_id, "thread/settings/update", {"threadId": thread_id, **params}
        )
