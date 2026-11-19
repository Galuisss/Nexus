"""Read the visible record prefix fixed by Codex's saved fork boundary."""

import json
from copy import deepcopy

from openai_codex.types import ThreadTokenUsage

from .errors import NexusError


async def frozen_usage(service, machine_id: str, thread: dict) -> dict | None:
    end = None
    path = thread.get("path")
    while path:
        path, data = await service.message_history.read(machine_id, path, end)
        records = data.splitlines()
        for line in reversed(records):
            record = json.loads(line)
            payload = record.get("payload", {})
            if (
                record["type"] == "event_msg"
                and payload.get("type") == "token_count"
                and payload.get("info")
            ):
                info = payload["info"]
                return ThreadTokenUsage.model_validate(
                    {
                        "total": info["total_token_usage"],
                        "last": info["last_token_usage"],
                        "model_context_window": info.get("model_context_window"),
                    }
                ).model_dump(by_alias=True, exclude_none=True)
        boundary = json.loads(records[0])["payload"].get("history_base")
        if not boundary:
            return None
        path = await service.message_history.ancestor_path(machine_id, path, boundary["thread_id"])
        end = boundary["end_byte_offset"]
    return None


async def frozen_context(
    service, machine_id: str, thread: dict, fallback: list[dict]
) -> list[dict]:
    if not thread.get("path"):
        return fallback

    async def read(path: str) -> bytes:
        return (await service.message_history.read(machine_id, path))[1]

    data = await read(thread["path"])
    meta = json.loads(data.splitlines()[0])["payload"]
    parent_id = thread["id"]
    if boundary := meta.get("history_base"):
        path = await service.message_history.ancestor_path(
            machine_id, thread["path"], boundary["thread_id"]
        )
        data = (await read(path))[: boundary["end_byte_offset"]]
        parent_id = json.loads(data.splitlines()[0])["payload"]["id"]

    turns = {}
    pending = {}
    current = None
    for line in data.splitlines():
        record = json.loads(line)
        payload = record.get("payload", {})
        if record["type"] == "event_msg":
            if payload.get("type") == "task_started":
                current = payload["turn_id"]
                turns.setdefault(current, {})
            elif payload.get("type") == "item_completed":
                turn_id = payload["turn_id"]
                item = payload["item"]
                turns.setdefault(turn_id, {})[item["id"]] = item
        elif record["type"] == "response_item":
            kind = payload.get("type")
            if (
                not boundary
                and kind == "message"
                and payload.get("role") == "developer"
                and any(
                    "<side_conversation>" in part.get("text", "")
                    for part in payload.get("content", [])
                )
            ):
                break
            if kind in {"function_call", "custom_tool_call"}:
                pending[payload["call_id"]] = (current, payload)
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                # Injected messages such as goal_objective have no matching call.
                pending.pop(payload.get("call_id"), None)

    latest = next(
        (
            turn_id
            for turn_id in reversed(turns)
            if turns[turn_id] or any(turn == turn_id for turn, _ in pending.values())
        ),
        None,
    )
    if latest is None:
        return fallback
    cursor = None
    while True:
        page = await service.rpc(
            machine_id,
            "thread/turns/list",
            {
                "threadId": parent_id,
                "limit": 30,
                "itemsView": "full",
                "sortDirection": "desc",
                "cursor": cursor,
            },
        )
        turn = next((turn for turn in page["data"] if turn["id"] == latest), None)
        if turn is not None:
            break
        cursor = page.get("nextCursor")
        if not cursor:
            raise NexusError("找不到侧聊分叉位置对应的原始轮次", status_code=409)
    items = []
    for item in turn["items"]:
        if saved := turns[latest].get(item["id"]):
            item = deepcopy(item)
            if item["type"] == "commandExecution":
                item["aggregatedOutput"] = saved.get("aggregated_output", "")
                item["exitCode"] = saved.get("exit_code")
                item["status"] = saved["status"]
            items.append(item)
    ids = {item["id"] for item in items}
    for call_id, (turn_id, call) in pending.items():
        if turn_id == latest and call_id not in ids:
            items.append(
                {
                    "id": call_id,
                    "type": "functionCall",
                    "name": call["name"],
                    "arguments": call.get("arguments", call.get("input")),
                    "status": "inProgress",
                }
            )
    return [{"id": latest, "items": items}]
