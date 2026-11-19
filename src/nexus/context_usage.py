"""Attribute saved model context and reconcile native usage."""

import asyncio
import base64
import json
import re
import struct
from bisect import bisect_right
from collections import Counter, defaultdict
from functools import lru_cache

import tiktoken

from .context_records import tool_key

CATEGORIES = {
    "base": "模型基础指令",
    "developer": "运行时 developer 指令",
    "custom": "自定义 developer 指令",
    "agents": "AGENTS.md",
    "skills": "SKILL 全文",
    "user": "用户 prompt",
    "assistant": "模型回复",
    "tools": "工具定义",
    "tool_calls": "Tool call 调用",
    "tool_results": "Tool call 返回",
    "reasoning": "思考",
    "framing": "消息结构 token",
    "output_delta": "输出待对齐",
    "injected": "中途注入提示",
    "environment": "环境上下文",
    "compaction": "压缩上下文",
    "media": "图片与音频",
    "unused": "未使用上下文",
}

KIND_NAMES = {
    "host_skills.instructions": "可用技能目录",
    "skills.catalog": "可用技能目录",
    "permissions.instructions": "权限",
    "collaboration_mode.instructions": "协作模式",
    "tools.deferred_namespaces": "延迟加载工具",
    "plugins.recommendations": "推荐插件",
    "multi_agent.role_instructions": "多代理角色",
    "multi_agent.mode_instructions": "多代理模式",
}
MODEL_OUTPUTS = {
    "reasoning",
    "function_call",
    "custom_tool_call",
    "local_shell_call",
    "web_search_call",
    "image_generation_call",
    "tool_search_call",
    "agent_message",
}


@lru_cache(maxsize=8)
def encoding(model: str):
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return tiktoken.get_encoding("o200k_base")


def text_tokens(text: str, model: str) -> int:
    return len(encoding(model).encode_ordinary(text))


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def truncate_text(text: str, budget: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= budget:
        return text
    left = budget // 2
    right = budget - left
    omitted = (len(data) - budget + 3) // 4
    return (
        data[:left].decode("utf-8", errors="ignore")
        + f"…{omitted} tokens truncated…"
        + (data[-right:].decode("utf-8", errors="ignore") if right else "")
    )


def is_model_output(item: dict) -> bool:
    return item.get("type") in MODEL_OUTPUTS or item.get("role") == "assistant"


def recipient(item: dict) -> str:
    name = item.get("name") or "tool"
    if namespace := item.get("namespace"):
        return f"{namespace}.{name}"
    return name if "." in name else f"functions.{name}"


def message_frame(item: dict, tool_name: str = "tool") -> tuple[str, str]:
    kind = item.get("type")
    if kind == "message":
        role = item["role"]
        if role == "assistant":
            phase = "final" if item.get("phase") in {"final", "final_answer"} else "commentary"
            return (
                f"<|start|>assistant<|channel|>{phase}<|message|>",
                "<|return|>" if phase == "final" else "<|end|>",
            )
        return f"<|start|>{role}<|message|>", "<|end|>"
    if kind in {"function_call", "custom_tool_call"}:
        format = "code" if kind == "custom_tool_call" else "json"
        return (
            f"<|start|>assistant to={recipient(item)}<|channel|>analysis<|constrain|>{format}<|message|>",
            "<|call|>",
        )
    if kind in {"function_call_output", "custom_tool_call_output"}:
        return f"<|start|>{tool_name}<|message|>", "<|end|>"
    if kind == "additional_tools":
        return "<|start|>developer<|message|>", "<|end|>"
    return "", ""


def framing_costs(item: dict, tool_name: str = "tool") -> Counter:
    header, footer = message_frame(item, tool_name)
    enc = tiktoken.get_encoding("o200k_harmony")
    return Counter(
        {
            ("framing", label): len(enc.encode(value, allowed_special="all"))
            for label, value in (("消息头", header), (footer, footer))
            if value
        }
    )


def part_label(item, index, initial=False):
    role = item.get("role")
    value = item["content"][index].get("text", "")
    tags = item.get("internal_chat_message_metadata_passthrough") or {}
    kinds = tags.get("content_item_kinds") or []
    origins = tags.get("content_item_metadata") or []
    tag = kinds[index] if index < len(kinds) else ""
    origin = origins[index].get("provenance", {}) if index < len(origins) else {}
    if item["content"][index].get("type") not in {"input_text", "output_text", "text"}:
        return "media", "图片与音频"
    if tag == "model.base_instructions":
        return "base", "模型基础指令"
    if role == "developer" and tag == "generic.developer_instructions":
        return "custom", "自定义 developer 指令"
    if tag == "agents_md.instructions" or value.startswith("# AGENTS.md instructions"):
        directory = re.search(r"^# AGENTS.md instructions for ([^\n]+)", value)
        return "agents", directory[1] if directory else "AGENTS.md"
    if tag.startswith("environments.") or value.startswith("<environment_context>"):
        return "environment", "环境上下文"
    if tag not in {"host_skills.instructions", "skills.catalog"} and (
        tag.startswith(("skill.", "skills.")) or origin.get("type") == "skill"
    ):
        name = re.search(r"<(?:name|skill_name)>([^<]+)", value)
        return "skills", name[1] if name else "原生加载技能"
    if role == "assistant":
        return "assistant", "最终回复" if item.get("phase") in {
            "final_answer",
            "final",
        } else "进度与回复"
    if role == "developer":
        return "developer" if initial else "injected", KIND_NAMES.get(tag, tag or "Developer 消息")
    if tag and tag not in {"user.text", "unknown"}:
        return "injected", KIND_NAMES.get(tag, tag)
    return "user", "用户消息"


def joined_text_tokens(parts, model):
    """Attribute BPE tokens after joining adjacent text content without separators."""
    ends, size = [], 0
    for value in parts:
        size += len(value.encode("utf-8"))
        ends.append(size)
    counts = [0] * len(parts)
    offset = 0
    enc = encoding(model)
    for token in enc.encode_ordinary("".join(parts)):
        counts[bisect_right(ends, offset)] += 1
        offset += len(enc.decode_single_token_bytes(token))
    return counts


def item_costs(item: dict, metadata: dict, model: str, *, initial: bool = False) -> Counter:
    costs = Counter()

    def add(category, label, tokens):
        if tokens:
            costs[category, label] += tokens

    def text(category, label, value):
        add(category, label, text_tokens(value, model))

    def media(part):
        if part.get("type") in {"input_image", "image", "image_url"}:
            tokens = 10000 if part.get("detail") == "original" else 1844
            url = part.get("image_url", "")
            if part.get("detail") == "original" and url.startswith("data:image/png;base64,"):
                header = base64.b64decode(url.split(",", 1)[1][:48])
                width, height = struct.unpack(">II", header[16:24])
                tokens = min(10000, ((width + 31) // 32) * ((height + 31) // 32))
            add("media", "图片", tokens)
        elif part.get("type") in {"input_audio", "audio"}:
            add("media", "音频", 0)

    kind = item.get("type")
    if kind == "message":
        role = item.get("role")
        if role == "system":
            return costs
        content = item.get("content", [])
        index = 0
        while index < len(content):
            part = content[index]
            if part.get("type") not in {"input_text", "output_text", "text"}:
                media(part)
                index += 1
                continue
            end = index + 1
            while end < len(content) and content[end].get("type") in {
                "input_text",
                "output_text",
                "text",
            }:
                end += 1
            counts = joined_text_tokens([part["text"] for part in content[index:end]], model)
            for position, count in enumerate(counts, index):
                add(*part_label(item, position, initial), count)
            index = end
    elif kind in {"compaction", "context_compaction"}:
        length = len(item.get("encrypted_content") or "")
        add("compaction", "压缩记录", ((length + 2) // 3 + 3) // 4)
    elif kind in {"function_call", "custom_tool_call"}:
        name = item["name"]
        text("tool_calls", name, item.get("arguments", item.get("input", "")))
    elif kind in {"function_call_output", "custom_tool_call_output"}:
        label = item.get("name") or "工具返回"
        output = item.get("output", "")
        budget = metadata.get("fallback_token_limit_override", 12000) * 4
        if isinstance(output, str):
            text("tool_results", label, truncate_text(output, budget))
        else:
            omitted = 0
            for part in output:
                if part.get("type") in {"input_text", "output_text", "text"}:
                    value = part["text"]
                    if budget > 0:
                        text("tool_results", label, truncate_text(value, budget))
                        budget = max(0, budget - len(value.encode("utf-8")))
                    elif value:
                        omitted += 1
                elif part.get("type") == "encrypted_content":
                    length = (len(part["encrypted_content"]) * 9 + 15) // 16
                    add("tool_results", label, (length + 3) // 4)
                else:
                    media(part)
            if omitted:
                text("tool_results", label, f"[omitted {omitted} text items ...]")
    elif kind in {"additional_tools", "tool_search_output"}:
        text("tools", "工具定义", json_text(item.get("tools", [])))
    elif kind in {"tool_search_call", "local_shell_call", "web_search_call"}:
        text("tool_calls", kind, json_text(item.get("arguments", item.get("action", {}))))
    elif kind == "agent_message":
        for part in item.get("content", []):
            if part.get("type") == "input_text":
                text("injected", "代理消息", part["text"])
            elif part.get("type") == "encrypted_content":
                add("injected", "代理消息", (len(part["encrypted_content"]) * 9 + 63) // 64)
    elif kind == "image_generation_call":
        text("tool_calls", "图片生成", item.get("revised_prompt") or "")
        if item.get("result"):
            add("media", "生成图片", 1844)
    return costs


class ContextAnalysis:
    def __init__(self, native=None):
        native = native or {}
        self.native_responses = native.get("responses", {})
        self.native_items = native.get("items", {})
        self.native_tools = native.get("tools", {})
        self.items = []
        self.outputs = []
        self.samples = []
        self.latest = None
        self.model = ""
        self.base = ""
        self.window = None
        self.window_number = 0
        self.through = 0
        self.has_sampled = False
        self.response_id = None
        self.thread_id = None
        self.reasoning_usage = {}
        self.reasoning_items = {}
        self.call_names = {}
        self.native_content_tokens = {}
        self.previous = None
        self.has_native_usage = False

    def add(self, item, metadata=None):
        if item.get("role") == "system":
            return
        metadata = metadata or {}
        entry = {
            "item": item,
            "metadata": metadata,
            "costs": item_costs(item, metadata, self.model, initial=not self.has_sampled),
            "initial": not self.has_sampled,
        }
        if item.get("type") in {"function_call", "custom_tool_call"}:
            self.call_names[item["call_id"]] = recipient(item)
        entry["frame"] = framing_costs(item, self.call_names.get(item.get("call_id"), "tool"))
        if item.get("type") in {"additional_tools", "tool_search_output"}:
            tokens = self.native_tools.get((self.model, tool_key(item)))
            if tokens is not None:
                entry["costs"] = Counter({("tools", "工具定义"): tokens})
                entry["frame"] = Counter()
        if item.get("id") in self.reasoning_items:
            entry["reasoningResponse"] = self.reasoning_items[item["id"]]
        self.items.append(entry)
        self.through = max(self.through, metadata.get("sampled_through_sequence", 0))
        if is_model_output(item):
            self.outputs.append(entry)
        return entry

    def native_costs(self, entry, attribution, field):
        item = entry["item"]
        total = attribution.get(field, 0)
        if not total or item.get("role") == "system":
            return Counter()
        content = attribution.get("content")
        if (
            item.get("type") == "message"
            and content
            and len(content) == len(item.get("content", []))
        ):
            costs = Counter()
            for index, part in enumerate(content):
                costs[part_label(item, index, entry.get("initial", False))] += part.get(field, 0)
            costs["framing", "消息结构"] += total - sum(costs.values())
            return costs
        kind = item.get("type")
        if kind == "reasoning":
            label = "reasoning", "原生推理用量"
        elif kind in {"additional_tools", "tool_search_output"}:
            label = "tools", "工具定义"
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            label = "tool_results", self.call_names.get(item.get("call_id"), "工具返回")
        elif kind in MODEL_OUTPUTS:
            label = "tool_calls", item.get("name", kind)
        elif kind in {"compaction", "context_compaction"}:
            label = "compaction", "压缩上下文"
        else:
            costs = entry.get("costs", {})
            if not costs:
                return Counter()
            label = next(iter(costs))
        return Counter({label: total})

    def native_ledger(self, attribution, outputs):
        entries = {entry["item"]["id"]: entry for entry in self.items if entry["item"].get("id")}
        retained = {entry["item"].get("id") for entry in outputs}
        inputs, generated, cached, written = Counter(), Counter(), Counter(), Counter()
        discarded = 0
        for item_id, usage in attribution.items():
            entry = entries.get(item_id) or {"item": self.native_items.get(item_id, {})}
            for field, costs in (
                ("input_tokens", inputs),
                ("cached_tokens", cached),
                ("cache_write_tokens", written),
            ):
                if usage.get("output_tokens") and usage.get("input_tokens", 0) <= 2:
                    costs["framing", "assistant 预填充"] += usage.get(field, 0)
                else:
                    costs.update(self.native_costs(entry, usage, field))
            if item_id in retained:
                generated.update(self.native_costs(entry, usage, "output_tokens"))
            else:
                discarded += usage.get("output_tokens", 0)
        return inputs, generated, cached, written, discarded

    def snapshot(self, usage, timestamp, turn_id, response_id=None):
        # A tool result may be recorded before token_count even though the request
        # predates it. Native sequences select exactly the sampled input prefix.
        sequence_known = self.through > 0
        inputs = [
            entry
            for entry in self.items
            if sequence_known and entry["metadata"].get("history_sequence", 0) <= self.through
        ]
        outputs = [
            entry
            for entry in self.outputs
            if not sequence_known or entry["metadata"].get("history_sequence", 0) > self.through
        ]
        if not sequence_known:
            output_ids = {id(entry) for entry in outputs}
            inputs = [entry for entry in self.items if id(entry) not in output_ids]
        response = response_id or f"usage-{len(self.samples) + 1}"
        reasoning_items = [entry for entry in outputs if entry["item"]["type"] == "reasoning"]
        if not reasoning_items and outputs:
            reasoning_items = outputs[-1:]
        self.reasoning_usage[response] = usage.get("reasoning_output_tokens", 0)
        for entry in reasoning_items:
            entry["reasoningResponse"] = response
            if item_id := entry["item"].get("id"):
                self.reasoning_items[item_id] = response
        input_costs = Counter()
        for entry in inputs:
            input_costs.update(entry["costs"])
            input_costs.update(entry["frame"])
        responses = {entry["reasoningResponse"] for entry in inputs if "reasoningResponse" in entry}
        input_costs["reasoning", "原生推理用量"] = sum(
            self.reasoning_usage[key] for key in responses
        )
        input_costs["framing", "历史思考消息头"] = sum(
            2 for key in responses if self.reasoning_usage[key]
        )
        input_costs["framing", "assistant 预填充"] = 2
        if self.base and not any(category == "base" for category, _ in input_costs):
            input_costs["base", "模型基础指令"] = text_tokens(self.base, self.model)
            input_costs["framing", "消息头"] += 4
        output_costs = Counter()
        for entry in outputs:
            output_costs.update(entry["costs"])
            frame = entry["frame"].copy()
            item = entry["item"]
            if item.get("role") == "assistant" and item.get("id") in self.native_content_tokens:
                native = self.native_content_tokens[item["id"]]
                body = sum(entry["costs"].values())
                _, footer = message_frame(item)
                if native - body in {0, 1}:
                    frame["framing", footer] = native - body
            output_costs.update(frame)
        output_costs["reasoning", "原生推理用量"] = usage.get("reasoning_output_tokens", 0)
        if outputs and outputs[0]["item"]["type"] != "reasoning":
            output_costs["framing", "assistant 预填充"] = -2
        attribution = self.native_responses.get(response_id)
        native_discarded = None
        if attribution is not None:
            input_costs, output_costs, cached_costs, written_costs, native_discarded = (
                self.native_ledger(attribution, outputs)
            )
        input_known, output_known = sum(input_costs.values()), sum(output_costs.values())
        input_delta = usage["input_tokens"] - input_known
        output_delta = usage["output_tokens"] - output_known
        # Incomplete responses can report sampled output without committing a
        # message or call. Spending stays in the response ledger, outside context.
        if native_discarded is not None:
            discarded = native_discarded
        elif all(entry["item"]["type"] == "reasoning" for entry in outputs):
            discarded = max(0, output_delta)
        else:
            discarded = 0
        output_delta -= discarded
        costs = input_costs.copy()
        costs.update(output_costs)
        costs["output_delta", "输出差额"] = output_delta
        known = input_known + output_known
        used = usage["total_tokens"] - discarded
        cache = {
            "cachedTokens": usage.get("cached_input_tokens", 0),
            "freshTokens": usage["input_tokens"] - usage.get("cached_input_tokens", 0),
            "knownTokens": sum(cached_costs.values()) if attribution is not None else None,
            "delta": usage.get("cached_input_tokens", 0) - sum(cached_costs.values())
            if attribution is not None
            else None,
            "categories": self.cost_rows(cached_costs) if attribution is not None else [],
            "writeTokens": usage.get("cache_write_input_tokens", 0),
            "writeKnownTokens": sum(written_costs.values()) if attribution is not None else None,
            "writeDelta": usage.get("cache_write_input_tokens", 0) - sum(written_costs.values())
            if attribution is not None
            else None,
            "previousContextTokens": None,
            "boundaryDelta": None,
            "sharedItems": 0,
        }
        if self.previous and self.previous["model"] == self.model:
            previous_entries = self.previous["entries"]
            shared = 0
            for before, now in zip(previous_entries, inputs):
                if before is not now:
                    break
                shared += 1
            cache["sharedItems"] = shared
            if shared == len(previous_entries):
                cache["previousContextTokens"] = self.previous["tokens"]
                cache["boundaryDelta"] = self.previous["tokens"] - cache["cachedTokens"]
                if self.previous["discarded"] and self.previous["inputDelta"] == input_delta:
                    self.samples[-1]["discardedConfirmed"] = True
        snapshot = {
            "timestamp": timestamp,
            "turnId": turn_id,
            "responseId": response_id,
            "threadId": self.thread_id,
            "model": self.model,
            "windowNumber": self.window_number,
            "modelContextWindow": self.window,
            "usedTokens": used,
            "nativeTotalTokens": usage["total_tokens"],
            "discardedOutputTokens": discarded,
            "attribution": "native" if attribution is not None else "estimated",
            "knownTokens": known,
            "usage": usage,
            "sampledThrough": self.through if sequence_known else None,
            "input": {
                "nativeTokens": usage["input_tokens"],
                "knownTokens": input_known,
                "delta": input_delta,
                "categories": self.cost_rows(input_costs),
            },
            "output": {
                "nativeTokens": usage["output_tokens"],
                "knownTokens": output_known,
                "discardedTokens": discarded,
                "delta": output_delta,
                "categories": self.cost_rows(output_costs),
                "items": [
                    {
                        "id": entry["item"].get("id"),
                        "type": entry["item"]["type"],
                        "bodyTokens": sum(entry["costs"].values()),
                        "header": message_frame(entry["item"])[0],
                        "footer": message_frame(entry["item"])[1],
                        "contentTokenCount": self.native_content_tokens.get(
                            entry["item"].get("id")
                        ),
                    }
                    for entry in outputs
                ],
            },
            "cache": cache,
            "categories": self.categories(costs, used),
        }
        self.samples.append(
            {
                key: value
                for key, value in snapshot.items()
                if key not in {"categories", "input", "output"}
            }
            | {
                "inputTokens": usage["input_tokens"],
                "inputKnownTokens": input_known,
                "inputDelta": input_delta,
                "outputTokens": usage["output_tokens"],
                "outputKnownTokens": output_known,
                "outputDelta": output_delta,
                "discardedConfirmed": False,
            }
        )
        self.latest = snapshot
        self.previous = {
            "entries": inputs + outputs,
            "model": self.model,
            "tokens": used,
            "inputDelta": input_delta,
            "discarded": discarded,
        }
        self.outputs = []
        self.has_sampled = True
        self.response_id = response_id

    def cost_rows(self, costs):
        totals = Counter()
        for (category, _), tokens in costs.items():
            totals[category] += tokens
        return [
            {"id": key, "label": CATEGORIES[key], "tokens": tokens}
            for key, tokens in totals.items()
            if tokens
        ]

    def categories(self, costs, used=None):
        totals = Counter()
        children = defaultdict(list)
        for (category, label), tokens in costs.items():
            totals[category] += tokens
            if tokens:
                children[category].append({"label": label, "tokens": tokens})
        totals["unused"] = (
            self.window - used if self.window is not None and used is not None else None
        )
        return [
            {
                "id": key,
                "label": label,
                "tokens": totals[key],
                "children": sorted(children[key], key=lambda row: -row["tokens"]),
            }
            for key, label in CATEGORIES.items()
        ]

    def feed(self, record):
        kind, payload = record["type"], record.get("payload", {})
        if kind == "session_meta":
            self.thread_id = payload.get("id", self.thread_id)
            base = payload.get("base_instructions") or ""
            if base:
                self.base = base if isinstance(base, str) else base.get("text", "")
        elif kind == "turn_context":
            self.model = payload.get("model", self.model)
        elif kind == "response_item":
            self.add(payload, record.get("metadata"))
        elif kind == "compacted" and payload.get("replacement_history") is not None:
            self.items = []
            self.outputs = []
            self.latest = None
            self.previous = None
            self.window_number += 1
            metadata = payload.get("replacement_history_metadata") or []
            for index, item in enumerate(payload["replacement_history"]):
                self.add(item, metadata[index] if index < len(metadata) else None)
            self.outputs = []
        elif kind == "token_usage_record":
            self.has_native_usage = True
            self.thread_id = payload.get("thread_id", self.thread_id)
            self.snapshot(
                payload["usage"],
                record.get("timestamp"),
                payload.get("turn_id"),
                payload.get("response_id"),
            )
        elif kind == "event_msg" and payload.get("type") == "token_count" and payload.get("info"):
            info = payload["info"]
            self.window = info.get("model_context_window")
            if not self.has_native_usage and (
                self.latest is None or self.latest["usage"] != info["last_token_usage"]
            ):
                self.snapshot(info["last_token_usage"], record.get("timestamp"), None)
            if self.latest is None:
                return
            self.latest["modelContextWindow"] = self.window
            self.samples[-1]["modelContextWindow"] = self.window
            self.latest["categories"][-1]["tokens"] = (
                self.window - self.latest["usedTokens"] if self.window is not None else None
            )

    def result(self, thread_id=None):
        thread_id = thread_id or self.thread_id
        if self.latest:
            snapshot = self.latest
        else:
            costs = Counter()
            for entry in self.items:
                costs.update(entry["costs"])
            snapshot = {
                "model": self.model,
                "modelContextWindow": self.window,
                "usedTokens": None,
                "knownTokens": sum(costs.values()),
                "categories": self.categories(costs),
            }
        return {
            **snapshot,
            "threadId": thread_id,
            "windowNumber": self.window_number,
            "source": "saved" if self.latest else "preview",
            "tokenizer": encoding(snapshot["model"]).name,
            "samples": self.samples,
        }


def analyze_records(
    records: list[dict], thread_id: str | None = None, native: dict | None = None
) -> dict:
    analysis = ContextAnalysis(native)
    # Initial tool and instruction items precede the first turn_context record.
    analysis.model = next(
        (
            record["payload"]["model"]
            for record in records
            if record["type"] == "turn_context" and record["payload"].get("model")
        ),
        "",
    )
    for record in records:
        payload = record.get("payload", {})
        if (
            record["type"] == "event_msg"
            and payload.get("type") == "item_completed"
            and payload.get("content_token_count") is not None
        ):
            analysis.native_content_tokens[payload["item"]["id"]] = payload["content_token_count"]
    for record in records:
        analysis.feed(record)
    return analysis.result(thread_id)


async def context_usage(service, machine_id: str, thread_id: str) -> dict:
    await service.message_history.ready(machine_id, thread_id)
    thread = (await service.read_thread(machine_id, thread_id))["thread"]
    path, end = thread.get("path"), None
    fresh = service.sessions.fresh_threads.get((machine_id, thread_id))
    if fresh is not None and not fresh.get("turns"):
        path = None
    histories = []
    while path:
        path, data = await service.message_history.read(machine_id, path, end)
        # The recorder can still be appending its final JSONL line.
        records = [json.loads(line) for line in data[: data.rfind(b"\n") + 1].splitlines()]
        histories.append(records)
        boundary = records[0]["payload"].get("history_base")
        if not boundary:
            break
        path = await service.message_history.ancestor_path(machine_id, path, boundary["thread_id"])
        end = boundary["end_byte_offset"]
    records = [record for history in reversed(histories) for record in history]
    threads = {thread_id} | {
        record["payload"].get("thread_id")
        for record in records
        if record["type"] == "token_usage_record"
    }
    native = await asyncio.to_thread(service.context_records.read, machine_id, threads)
    if not records and not service.demo:
        from .developer_inputs import preview_inputs

        records = [
            {"type": "response_item", "payload": item}
            for item in await preview_inputs(service, machine_id, thread_id)
        ]
    return await asyncio.to_thread(analyze_records, records, thread_id, native)
