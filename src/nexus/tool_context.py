"""Per-conversation tool IDs and compacting on the native Responses request path."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from collections import defaultdict
from copy import deepcopy
from uuid import uuid4

import httpx
import zstandard
from starlette.responses import Response, StreamingResponse

from .errors import NexusError
from .network import REVERSE_PORT
from .tool_call_prompts import (
    MODE_RULES,
    PROMPT_VERSION,
    READ_ORIGINAL_TOOL,
    RETENTION_POLICY,
    compression_control,
)
from .tool_call_protocol import (
    apply_decision,
    format_tool_output,
    locate_spans,
    parse_decisions,
    render_ranges,
    text_body,
    text_projection_supported,
    text_tokens,
)

logger = logging.getLogger(__name__)
CALLS = {"function_call", "custom_tool_call"}
OUTPUTS = {"function_call_output", "custom_tool_call_output"}
MIN_TOKENS = 2048


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def is_tool_output(item):
    # Native goal_objective outputs have no corresponding tool call.
    return item.get("type") in OUTPUTS and "call_id" in item


class ToolContext:
    def __init__(self, service):
        self.service = service
        self.db, self.lock = service.database.connection, service.database.lock
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=30))
        self.serving_locks = defaultdict(asyncio.Lock)
        with self.lock, self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS tool_context_threads (
                    token TEXT PRIMARY KEY, machine TEXT NOT NULL, thread TEXT,
                    parent TEXT, compact INTEGER NOT NULL, counter INTEGER NOT NULL DEFAULT 0,
                    initialized INTEGER NOT NULL DEFAULT 0, rebase INTEGER NOT NULL DEFAULT 0,
                    meta TEXT NOT NULL, UNIQUE(machine,thread)
                );
                CREATE TABLE IF NOT EXISTS tool_context_calls (
                    token TEXT NOT NULL, call_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    item TEXT NOT NULL, original TEXT, projected TEXT, turn_id TEXT,
                    refetch INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(token,call_id)
                );
                CREATE INDEX IF NOT EXISTS tool_context_ordinal ON tool_context_calls(token,ordinal);
                CREATE TABLE IF NOT EXISTS tool_context_marks (
                    token TEXT NOT NULL, item_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    PRIMARY KEY(token,item_id)
                );
                CREATE TABLE IF NOT EXISTS tool_context_compactions (
                    id TEXT PRIMARY KEY, machine TEXT NOT NULL, thread TEXT NOT NULL,
                    turn_id TEXT NOT NULL, before_item TEXT, item TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tool_context_reads (
                    id INTEGER PRIMARY KEY, token TEXT NOT NULL, turn_id TEXT, event TEXT NOT NULL
                );
            """)
            self.db.execute(
                "UPDATE tool_context_compactions SET item=json_set(item,'$.status','interrupted','$.completedAtMs',?) WHERE json_extract(item,'$.status')='inProgress'",
                (int(time.time() * 1000),),
            )

    async def close(self):
        await self.http.aclose()

    def features(self):
        return {"toolCallId": self.service.store.data.get("features", {}).get("toolCallId", False)}

    async def update_features(self, values):
        if set(values) != {"toolCallId"} or type(values["toolCallId"]) is not bool:
            raise NexusError("Tool call ID 必须是开关值")
        self.service.store.data["features"] = {
            **self.service.store.data.get("features", {}),
            **values,
        }
        self.service.store.save()
        await self.service.events.publish(
            "", "nexus/features/updated", {"features": self.features()}
        )
        return self.features()

    def thread(self, machine, thread):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM tool_context_threads WHERE machine=? AND thread=?", (machine, thread)
            ).fetchone()
        return dict(row) if row else None

    def by_token(self, token):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM tool_context_threads WHERE token=?", (token,)
            ).fetchone()
        if not row:
            raise NexusError("找不到 Tool call 会话", status_code=404)
        return dict(row)

    def settings(self, machine, thread):
        state = self.thread(machine, thread)
        return {"toolCallId": bool(state), "toolCallCompact": bool(state and state["compact"])}

    def create(self, machine, meta, parent=None, thread=None):
        token = uuid4().hex
        compact = bool(self.service.store.data.get("toolCallCompactDefault", False))
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO tool_context_threads(token,machine,thread,parent,compact,meta) VALUES (?,?,?,?,?,?)",
                (token, machine, thread, parent, int(compact), encode(meta)),
            )
        return self.by_token(token)

    def bind(self, token, thread):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE tool_context_threads SET thread=? WHERE token=?", (thread, token)
            )

    def discard(self, token):
        with self.lock, self.db:
            self.db.execute(
                "DELETE FROM tool_context_threads WHERE token=? AND thread IS NULL", (token,)
            )

    async def default_upstream(self, machine_id):
        account = (
            {"type": "chatgpt"}
            if self.service.demo
            else (await self.service.rpc(machine_id, "account/read", {})).get("account") or {}
        )
        return (
            "https://chatgpt.com/backend-api/codex"
            if account.get("type") == "chatgpt"
            else "https://api.openai.com/v1"
        )

    async def configure(self, machine_id, method, params):
        if method not in {"thread/start", "thread/fork", "thread/resume"}:
            return params, None
        source = self.thread(machine_id, params.get("threadId"))
        if method == "thread/start":
            if params.get("ephemeral") or not self.features()["toolCallId"]:
                return params, None
            config = (
                {}
                if self.service.demo
                else (
                    await self.service.rpc(
                        machine_id,
                        "config/read",
                        {"cwd": params["cwd"]} if params.get("cwd") else {},
                    )
                )["config"]
            )
            effective = {**config, **(params.get("config") or {})}
            origin_id = params.get("modelProvider") or effective.get("model_provider") or "openai"
            provider = deepcopy((effective.get("model_providers") or {}).get(origin_id, {}))
            if provider.get("wire_api", "responses") != "responses" or provider.get("aws"):
                raise NexusError("Tool call ID 需要 Responses 模型接口")
            upstream = provider.get("base_url")
            if origin_id == "openai":
                upstream = effective.get("openai_base_url") or upstream
            if not upstream:
                upstream = await self.default_upstream(machine_id)
            instructions = params.get("developerInstructions")
            if instructions is None:
                instructions = effective.get("developer_instructions") or ""
            meta = {
                "upstream": upstream,
                "originProvider": origin_id,
                "instructions": MODE_RULES + ("\n\n" + instructions if instructions else ""),
                "promptVersion": PROMPT_VERSION,
            }
            state = self.create(machine_id, meta)
        elif method == "thread/fork":
            if not source:
                return params, None
            state = self.create(machine_id, json.loads(source["meta"]), parent=source["token"])
        else:
            if not source:
                return params, None
            state = source
        created_token = state["token"] if method != "thread/resume" else None
        return self.params(state, params, method), created_token

    def params(self, state, params, method):
        meta = json.loads(state["meta"])
        machine = self.service.machine(state["machine"])
        base = (
            f"http://127.0.0.1:{REVERSE_PORT}"
            if machine.get("transport") == "ssh"
            else self.service.nexus_url
        )
        proxy_base = f"{base}/api/tool-context/{state['token']}/backend-api/codex"
        config = dict(params.get("config") or {})
        provider_id = meta["originProvider"]
        if provider_id == "openai":
            config["openai_base_url"] = proxy_base
        else:
            config[f"model_providers.{provider_id}.base_url"] = proxy_base
            config[f"model_providers.{provider_id}.supports_websockets"] = False
        result = {
            **params,
            "modelProvider": provider_id,
            "config": config,
            "developerInstructions": meta["instructions"],
        }
        if method == "thread/start":
            tools = list(params.get("dynamicTools") or [])
            tool = deepcopy(READ_ORIGINAL_TOOL)
            if tools and any("type" not in entry for entry in tools):
                tool.pop("type")
            tools.append(tool)
            result["dynamicTools"] = tools
        return result

    async def set_compact(self, machine, thread, value):
        if type(value) is not bool:
            raise NexusError("Tool call compact 必须是开关值")
        if not self.thread(machine, thread):
            raise NexusError("这个会话没有启用 Tool call ID", status_code=409)
        with self.lock, self.db:
            self.db.execute(
                "UPDATE tool_context_threads SET compact=? WHERE machine=? AND thread=?",
                (int(value), machine, thread),
            )
        self.service.store.data["toolCallCompactDefault"] = value
        self.service.store.save()
        settings = self.settings(machine, thread)
        await self.service.events.publish(
            machine, "thread/settings/updated", {"threadId": thread, "threadSettings": settings}
        )
        return settings

    def find_call(self, token, call_id):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM tool_context_calls WHERE token=? AND call_id=?", (token, call_id)
            ).fetchone()
        return dict(row) if row else None

    def ancestor_call(self, state, call_id):
        parent = state["parent"]
        while parent:
            if row := self.find_call(parent, call_id):
                return row
            parent = self.by_token(parent)["parent"]
        return None

    def register_call(self, state, item, turn_id):
        if row := self.find_call(state["token"], item["call_id"]):
            if row["item"] != encode(item):
                with self.lock, self.db:
                    self.db.execute(
                        "UPDATE tool_context_calls SET item=? WHERE token=? AND call_id=?",
                        (encode(item), state["token"], item["call_id"]),
                    )
            return row
        with self.lock, self.db:
            self.db.execute(
                "UPDATE tool_context_threads SET counter=counter+1 WHERE token=?", (state["token"],)
            )
            number = self.db.execute(
                "SELECT counter FROM tool_context_threads WHERE token=?", (state["token"],)
            ).fetchone()[0]
            self.db.execute(
                "INSERT INTO tool_context_calls(token,call_id,ordinal,item,turn_id) VALUES (?,?,?,?,?)",
                (state["token"], item["call_id"], number, encode(item), turn_id),
            )
        return self.find_call(state["token"], item["call_id"])

    def initialize_history(self, state, items):
        if state["initialized"] and not state["rebase"]:
            return
        token = state["token"]
        for item in items:
            if item.get("type") not in CALLS and not is_tool_output(item):
                continue
            if self.find_call(token, item["call_id"]):
                continue
            inherited = self.ancestor_call(state, item["call_id"])
            if inherited:
                with self.lock, self.db:
                    self.db.execute(
                        "INSERT INTO tool_context_calls VALUES (?,?,?,?,?,?,?,?)",
                        (
                            token,
                            inherited["call_id"],
                            inherited["ordinal"],
                            inherited["item"],
                            inherited["original"],
                            inherited["projected"],
                            inherited["turn_id"],
                            inherited["refetch"],
                        ),
                    )
        known = [
            self.find_call(token, i["call_id"])
            for i in items
            if i.get("type") in CALLS or is_tool_output(i)
        ]
        boundary = max((r["ordinal"] for r in known if r), default=0)
        parent = token if state["rebase"] else state["parent"]
        while parent:
            with self.lock:
                for item in items:
                    key = item.get("encrypted_content") or item.get("id")
                    if not key:
                        continue
                    mark = self.db.execute(
                        "SELECT ordinal FROM tool_context_marks WHERE token=? AND item_id=?",
                        (parent, key),
                    ).fetchone()
                    if mark:
                        boundary = max(boundary, mark[0])
            parent = self.by_token(parent)["parent"]
        with self.lock, self.db:
            if state["parent"] and boundary:
                self.db.execute(
                    "INSERT OR IGNORE INTO tool_context_calls SELECT ?,call_id,ordinal,item,original,projected,turn_id,refetch FROM tool_context_calls WHERE token=? AND ordinal<=?",
                    (token, state["parent"], boundary),
                )
            if state["rebase"]:
                self.db.execute(
                    "DELETE FROM tool_context_calls WHERE token=? AND ordinal>?", (token, boundary)
                )
            self.db.execute(
                "UPDATE tool_context_threads SET counter=?,initialized=1,rebase=0 WHERE token=?",
                (boundary, token),
            )

    def prepare_input(self, state, payload, turn_id):
        items = deepcopy(payload.get("input", []))
        self.initialize_history(state, items)
        visible_calls = {i["call_id"] for i in items if i.get("type") in CALLS or is_tool_output(i)}
        with self.lock, self.db:
            pending = self.db.execute(
                "SELECT call_id FROM tool_context_calls WHERE token=? AND original IS NULL",
                (state["token"],),
            ).fetchall()
            folded = any(i.get("type") == "compaction" for i in items)
            abandoned = [r[0] for r in pending if r[0] not in visible_calls] if not folded else []
            if abandoned:
                self.db.executemany(
                    "DELETE FROM tool_context_calls WHERE token=? AND call_id=?",
                    [(state["token"], call_id) for call_id in abandoned],
                )
                self.db.execute(
                    "UPDATE tool_context_threads SET counter=(SELECT coalesce(max(ordinal),0) FROM tool_context_calls WHERE token=?) WHERE token=?",
                    (state["token"], state["token"]),
                )
        for item in items:
            if item.get("type") in CALLS:
                self.register_call(state, item, turn_id)
                code = item.get("input") or item.get("arguments") or ""
                if item.get("name") == "read_original" or "read_original" in code:
                    with self.lock, self.db:
                        receipt = self.db.execute(
                            "SELECT 1 FROM tool_context_reads WHERE token=? AND turn_id=? LIMIT 1",
                            (state["token"], turn_id),
                        ).fetchone()
                        if receipt:
                            self.db.execute(
                                "UPDATE tool_context_calls SET refetch=1 WHERE token=? AND call_id=?",
                                (state["token"], item["call_id"]),
                            )
        candidates = []
        for index, item in enumerate(items):
            if not is_tool_output(item):
                continue
            row = self.find_call(state["token"], item["call_id"])
            if not row:
                continue
            sid = f"r{row['ordinal']}"
            if row["original"] is None:
                marked = format_tool_output(item, sid)
                with self.lock, self.db:
                    self.db.execute(
                        "UPDATE tool_context_calls SET original=?,projected=? WHERE token=? AND call_id=?",
                        (encode(item), encode(marked), state["token"], item["call_id"]),
                    )
                row = self.find_call(state["token"], item["call_id"])
                if (
                    not row["refetch"]
                    and text_projection_supported(item)
                    and text_tokens(text_body(item)) >= MIN_TOKENS
                ):
                    candidates.append({"source_id": sid, "item_index": index, "original": item})
            items[index] = json.loads(row["projected"])
        first_call = next(
            (self.find_call(state["token"], i["call_id"]) for i in items if i.get("type") in CALLS),
            None,
        )
        offset = (
            first_call["ordinal"] - 1 if first_call else self.by_token(state["token"])["counter"]
        )
        if offset:
            checkpoint = {
                "type": "message",
                "role": "developer",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"Tool-call sequence offset: {offset}. Add this offset when numbering the retained top-level function and custom calls below.",
                    }
                ],
            }
            position = 0
            while position < len(items) and (
                items[position].get("type") == "additional_tools"
                or items[position].get("role") == "developer"
            ):
                position += 1
            items.insert(position, checkpoint)
            for candidate in candidates:
                if candidate["item_index"] >= position:
                    candidate["item_index"] += 1
        return {**payload, "input": items}, candidates

    async def read_original(self, machine, params):
        state = self.thread(machine, params["threadId"])
        arguments = params["arguments"]
        sid = arguments["source_id"]
        number = int(sid.removeprefix("r"))
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM tool_context_calls WHERE token=? AND ordinal=? AND original IS NOT NULL ORDER BY rowid DESC LIMIT 1",
                (state["token"], number),
            ).fetchone()
        if not row:
            raise NexusError("找不到这条工具返回的原文")
        original = json.loads(row["original"])
        raw = text_body(original)
        whole = all(arguments.get(key) is None for key in ("start", "end")) and not arguments.get(
            "anchors"
        )
        if whole and not text_projection_supported(original):
            content = []
            for block in original["output"]:
                if block["type"] == "input_text":
                    content.append({"type": "inputText", "text": block["text"]})
                elif block["type"] == "input_image":
                    content.append({"type": "inputImage", "imageUrl": block["image_url"]})
                elif block["type"] == "input_audio":
                    content.append({"type": "inputAudio", "audioUrl": block["audio_url"]})
        else:
            content = None
        anchors = arguments.get("anchors") or None
        diagnostic = None
        if anchors:
            ranges, diagnostic = locate_spans(raw, anchors)
        else:
            start, end = arguments.get("start", 0), arguments.get("end", len(raw))
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 <= start <= len(raw)
                or end < start
            ):
                ranges, diagnostic = [], {"type": "invalid_range"}
            else:
                ranges = [{"start": start, "end": min(end, len(raw))}]
        if diagnostic:
            ranges = [{"start": 0, "end": len(raw)}]
        text = render_ranges(raw, ranges)
        with self.lock, self.db:
            # Code-mode callbacks can have a nested ID; preserve all pending outputs in that batch.
            self.db.execute(
                "UPDATE tool_context_calls SET refetch=1 WHERE token=? AND turn_id=?",
                (state["token"], params.get("turnId")),
            )
            event = {
                "source_id": sid,
                "call_id": params["callId"],
                "ranges": ranges,
                "content": text,
                "text_tokens": text_tokens(text),
                "diagnostic": diagnostic,
            }
            self.db.execute(
                "INSERT INTO tool_context_reads(token,turn_id,event) VALUES (?,?,?)",
                (state["token"], params.get("turnId"), encode(event)),
            )
        return {
            "contentItems": content
            if content is not None
            else [{"type": "inputText", "text": text}],
            "success": True,
        }

    def native_event(self, machine, method, params):
        if method not in {"thread/reverted", "item/started", "turn/completed"}:
            return
        thread = params.get("threadId") or params.get("thread", {}).get("id")
        if not self.thread(machine, thread):
            return
        if method == "thread/reverted":
            with self.lock, self.db:
                self.db.execute(
                    "UPDATE tool_context_threads SET rebase=1 WHERE machine=? AND thread=?",
                    (machine, thread),
                )
        if method == "item/started" and params.get("item", {}).get("type") != "toolCallCompaction":
            with self.lock, self.db:
                self.db.execute(
                    "UPDATE tool_context_compactions SET before_item=? WHERE machine=? AND thread=? AND turn_id=? AND before_item IS NULL AND json_extract(item,'$.completedAtMs') IS NOT NULL",
                    (params["item"]["id"], machine, thread, params["turnId"]),
                )
        if method == "turn/completed":
            self.annotate(machine, thread, [params["turn"]])

    def context_item(self, machine, thread, item):
        if is_tool_output(item) and (state := self.thread(machine, thread)):
            if (row := self.find_call(state["token"], item["call_id"])) and row["projected"]:
                return json.loads(row["projected"])
        return item

    def annotate(self, machine, thread, turns):
        state = self.thread(machine, thread)
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM tool_context_compactions WHERE machine=? AND thread=? ORDER BY rowid",
                (machine, thread),
            ).fetchall()
        for turn in turns:
            items = turn.get("items", [])
            ids = {i["id"] for i in items}
            for row in rows:
                if row["turn_id"] != turn["id"] or row["id"] in ids:
                    continue
                index = next(
                    (n for n, item in enumerate(items) if item["id"] == row["before_item"]),
                    len(items),
                )
                items.insert(index, self.compaction_item(state, json.loads(row["item"])))

    def compaction_item(self, state, item):
        sources = item["sources"]
        with self.lock:
            rows = self.db.execute(
                f"SELECT ordinal,item,original,projected FROM tool_context_calls WHERE token=? AND ordinal IN ({','.join('?' for _ in sources)})",
                (state["token"], *(int(sid.removeprefix("r")) for sid in sources)),
            ).fetchall()
        calls = {f"r{row['ordinal']}": row for row in rows}
        results = []
        for sid in sources:
            row = calls.get(sid)
            if row is None:
                continue
            original = json.loads(row["original"])
            result = {"sourceId": sid, "call": json.loads(row["item"]), "original": original}
            if item["status"] in {"failed", "interrupted"}:
                result["outcome"] = "fallback_failed"
            elif item["status"] == "completed":
                projected = json.loads(row["projected"])
                outcome = item.get("outcomes", {}).get(sid)
                if outcome is None:
                    failed = any(
                        d.get("source_id") == sid or sid in d.get("fallback_sources", [])
                        for d in item.get("diagnostics", [])
                    )
                    outcome = (
                        "fallback_failed"
                        if failed
                        else "keep"
                        if projected == format_tool_output(original, sid)
                        else "compacted"
                    )
                result["outcome"] = outcome
                if outcome == "compacted":
                    result["projected"] = projected
            results.append(result)
        return {**item, "results": results}

    async def compact(self, state, payload, candidates, headers, url, turn_id):
        item = {
            "id": f"tool-compact-{uuid4().hex}",
            "type": "toolCallCompaction",
            "status": "inProgress",
            "startedAtMs": int(time.time() * 1000),
            "sources": [c["source_id"] for c in candidates],
        }
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO tool_context_compactions(id,machine,thread,turn_id,item) VALUES (?,?,?,?,?)",
                (item["id"], state["machine"], state["thread"], turn_id, encode(item)),
            )
        await self.service.events.publish(
            state["machine"],
            "item/started",
            {
                "threadId": state["thread"],
                "turnId": turn_id,
                "item": self.compaction_item(state, item),
            },
        )
        control = compression_control(item["sources"])
        policy = {
            "role": "developer",
            "content": [{"type": "input_text", "text": RETENTION_POLICY}],
        }
        query = {**payload, "input": payload["input"] + [policy, control]}
        parts, tool_calls = [], []
        try:
            async with self.http.stream(
                "POST", url, headers=headers, content=encode(query)
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: ") or line[6:] == "[DONE]":
                        continue
                    event = json.loads(line[6:])
                    if event["type"] == "response.output_text.delta":
                        parts.append(event["delta"])
                    elif (
                        event["type"] == "response.output_item.done"
                        and event["item"].get("type") in CALLS
                    ):
                        tool_calls.append(event["item"])
                    elif event["type"] == "response.completed":
                        item["usage"] = event["response"].get("usage", {})
                    elif event["type"] in {"response.failed", "response.incomplete", "error"}:
                        raise RuntimeError("Tool call compaction serving failed")
            if "usage" not in item or tool_calls:
                raise RuntimeError("Tool call compaction did not return a completed decision")
            decisions, diagnostics = parse_decisions("".join(parts), item["sources"])
            projected = list(payload["input"])
            outcomes, updates = {}, []
            for candidate in candidates:
                sid, index = candidate["source_id"], candidate["item_index"]
                replacement, diagnostic = apply_decision(candidate["original"], sid, decisions[sid])
                if diagnostic:
                    diagnostics.append({**diagnostic, "source_id": sid})
                projected[index] = replacement
                failed = any(
                    d.get("source_id") == sid or sid in d.get("fallback_sources", [])
                    for d in diagnostics
                )
                outcomes[sid] = (
                    "fallback_growth"
                    if diagnostic and diagnostic["type"] == "token_growth"
                    else "fallback_failed"
                    if failed
                    else "keep"
                    if replacement == format_tool_output(candidate["original"], sid)
                    else "compacted"
                )
                updates.append((encode(replacement), state["token"], replacement["call_id"]))
            with self.lock, self.db:
                self.db.executemany(
                    "UPDATE tool_context_calls SET projected=? WHERE token=? AND call_id=?", updates
                )
            item.update(
                status="completed", decisions=decisions, diagnostics=diagnostics, outcomes=outcomes
            )
            payload = {**payload, "input": projected}
        except asyncio.CancelledError:
            item["status"] = "interrupted"
            raise
        except Exception as error:
            item.update(status="failed", error=str(error))
            logger.warning("Tool call compaction failed for %s: %s", state["thread"], error)
        finally:
            item["completedAtMs"] = int(time.time() * 1000)
            with self.lock, self.db:
                self.db.execute(
                    "UPDATE tool_context_compactions SET item=? WHERE id=?",
                    (encode(item), item["id"]),
                )
            await self.service.events.publish(
                state["machine"],
                "item/completed",
                {
                    "threadId": state["thread"],
                    "turnId": turn_id,
                    "item": self.compaction_item(state, item),
                },
            )
        return payload

    async def compact_connected(self, request, *args):
        task = asyncio.create_task(self.compact(*args))

        async def disconnected():
            while not await request.is_disconnected():
                await asyncio.sleep(0.1)

        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return None
            return await task
        finally:
            for pending in (task, watcher):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, watcher, return_exceptions=True)

    async def proxy(self, token, path, request):
        route = self.by_token(token)
        meta = json.loads(route["meta"])
        if not meta.get("upstream"):
            meta["upstream"] = await self.default_upstream(route["machine"])
            with self.lock, self.db:
                self.db.execute(
                    "UPDATE tool_context_threads SET meta=? WHERE token=?", (encode(meta), token)
                )
        suffix = path.removeprefix("backend-api/codex").lstrip("/")
        url = meta["upstream"].rstrip("/") + "/" + suffix
        if request.url.query:
            url += "?" + request.url.query
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower()
            not in {"host", "content-length", "content-encoding", "connection", "accept-encoding"}
        }
        body = await request.body()
        if request.headers.get("content-encoding") == "zstd":
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(body)) as reader:
                body = reader.read()
        payload = json.loads(body) if body else None
        state = route
        turn_id = None
        if payload is not None:
            metadata = payload.get("client_metadata", {})
            thread = metadata.get("thread_id") or metadata.get("session_id") or route["thread"]
            turn_id = metadata.get("turn_id") or self.service.sessions.active_turns.get(
                (route["machine"], thread)
            )
            if route["thread"] is None:
                self.bind(token, thread)
                state = self.by_token(token)
            elif thread and thread != route["thread"]:
                state = self.thread(route["machine"], thread) or self.create(
                    route["machine"], meta, parent=token, thread=thread
                )
        if payload is not None and suffix in {"responses", "responses/compact"}:
            async with self.serving_locks[state["token"]]:
                payload, candidates = self.prepare_input(state, payload, turn_id)
                if (
                    suffix == "responses"
                    and candidates
                    and self.by_token(state["token"])["compact"]
                ):
                    payload = await self.compact_connected(
                        request, state, payload, candidates, headers, url, turn_id
                    )
                    if payload is None:
                        return Response(status_code=499)
                for item in payload["input"]:
                    self.service.context_records.record_item(state["machine"], item)
                body = encode(payload).encode()
        if await request.is_disconnected():
            return Response(status_code=499)
        upstream = await self.http.send(
            self.http.build_request(request.method, url, headers=headers, content=body), stream=True
        )
        response_headers = {
            k: v
            for k, v in upstream.headers.items()
            if k.lower()
            not in {"content-length", "content-encoding", "transfer-encoding", "connection"}
        }
        # The Codex upstream can omit Content-Type on SSE responses.
        streaming = (
            suffix == "responses" and payload is not None and payload.get("stream") is True
        ) or "text/event-stream" in upstream.headers.get("content-type", "")
        if upstream.status_code >= 400 or not streaming:
            content = await upstream.aread()
            await upstream.aclose()
            if upstream.status_code < 400 and suffix == "responses/compact":
                compacted = json.loads(content)
                counter = self.by_token(state["token"])["counter"]
                with self.lock, self.db:
                    for item in compacted.get("output", []):
                        key = item.get("encrypted_content") or item.get("id")
                        if key:
                            self.db.execute(
                                "INSERT OR REPLACE INTO tool_context_marks VALUES (?,?,?)",
                                (state["token"], key, counter),
                            )
            return Response(content, status_code=upstream.status_code, headers=response_headers)

        response_headers.setdefault("content-type", "text/event-stream")
        response_headers["x-accel-buffering"] = "no"

        async def stream():
            try:
                async for line in upstream.aiter_lines():
                    if line.startswith("data: ") and line[6:] != "[DONE]":
                        event = json.loads(line[6:])
                        if event["type"] in {
                            "response.output_item.added",
                            "response.output_item.done",
                        }:
                            item = event["item"]
                            if item.get("type") in CALLS:
                                self.register_call(state, item, turn_id)
                            key = item.get("encrypted_content") or item.get("id")
                            if (
                                event["type"] == "response.output_item.done"
                                and key
                                and item.get("type") == "compaction"
                            ):
                                counter = self.by_token(state["token"])["counter"]
                                with self.lock, self.db:
                                    self.db.execute(
                                        "INSERT OR REPLACE INTO tool_context_marks VALUES (?,?,?)",
                                        (state["token"], key, counter),
                                    )
                    yield (line + "\n").encode()
            finally:
                await upstream.aclose()

        return StreamingResponse(
            stream(), status_code=upstream.status_code, headers=response_headers
        )
