"""Read saved developer inputs, or generate a native preview for a new thread."""

import json
from difflib import unified_diff

from .errors import NexusError


def field_changes(before: dict, after: dict, prefix: str = "") -> list[dict]:
    changes = []
    for key in sorted(before.keys() | after.keys()):
        field = f"{prefix}.{key}" if prefix else key
        if key in before and key in after:
            old, new = before[key], after[key]
            if old == new:
                continue
            if isinstance(old, dict) and isinstance(new, dict):
                changes.extend(field_changes(old, new, field))
                continue
            kind = "updated"
        else:
            old, new = before.get(key, ""), after.get(key, "")
            kind = "added" if key in after else "removed"
        old = old if isinstance(old, str) else json.dumps(old, ensure_ascii=False, indent=2)
        new = new if isinstance(new, str) else json.dumps(new, ensure_ascii=False, indent=2)
        changes.append(
            {
                "field": field,
                "kind": kind,
                "diff": "\n".join(
                    unified_diff(
                        old.splitlines(),
                        new.splitlines(),
                        fromfile="修改前",
                        tofile="修改后",
                        n=2,
                        lineterm="",
                    )
                ),
            }
        )
    return changes


def update_tools(definitions: list[dict], catalog: dict, namespace: str = "") -> list[dict]:
    updates = []
    for definition in definitions:
        name = namespace + (definition.get("name") or definition["type"])
        if definition["type"] == "namespace":
            updates.extend(update_tools(definition["tools"], catalog, name + "."))
            continue
        before = catalog.get(name)
        catalog[name] = definition
        if before is None:
            kind = "added"
        elif before != definition:
            kind = "updated"
        else:
            kind = "unchanged"
        updates.append(
            {
                "name": name,
                "kind": kind,
                "changes": field_changes(before, definition) if before is not None else [],
                "definition": definition,
            }
        )
    return updates


async def preview_inputs(service, machine_id: str, thread_id: str) -> list[dict]:
    state = await service.thread_state(machine_id, thread_id)
    settings, thread = state["settings"], state["thread"]
    machine = service.machine(machine_id)
    overrides = {
        "model": settings.get("model"),
        "model_provider": thread.get("modelProvider"),
        "model_reasoning_effort": settings.get("reasoningEffort"),
        "approval_policy": settings.get("approvalPolicy"),
    }
    if context := service.tool_context.thread(machine_id, thread_id):
        overrides["developer_instructions"] = json.loads(context["meta"])["instructions"]
    sandbox = settings.get("sandbox")
    if sandbox:
        overrides["sandbox_mode"] = (
            sandbox
            if isinstance(sandbox, str)
            else {
                "readOnly": "read-only",
                "workspaceWrite": "workspace-write",
                "dangerFullAccess": "danger-full-access",
            }.get(sandbox["type"])
        )
        if isinstance(sandbox, dict) and sandbox["type"] == "workspaceWrite":
            for native, config in (
                ("writableRoots", "writable_roots"),
                ("networkAccess", "network_access"),
                ("excludeTmpdirEnvVar", "exclude_tmpdir_env_var"),
                ("excludeSlashTmp", "exclude_slash_tmp"),
            ):
                if native in sandbox:
                    overrides[f"sandbox_workspace_write.{config}"] = sandbox[native]
    command = [machine.get("codexBin") or "codex", "debug", "prompt-input"]
    for name, value in overrides.items():
        if value is not None:
            command.extend(["-c", f"{name}={json.dumps(value, ensure_ascii=False)}"])
    if environment := machine.get("env"):
        command = ["env", *(f"{name}={value}" for name, value in environment.items()), *command]
    result = await service.commands.execute(machine_id, command, thread["cwd"], timeout_seconds=25)
    if result["timedOut"]:
        raise NexusError("生成 Developer 输入预览超时", status_code=504)
    if result["exitCode"]:
        raise NexusError(result["stderr"].strip() or "生成 Developer 输入预览失败", status_code=502)
    if result["stdoutTruncated"]:
        raise NexusError("Developer 输入预览超出命令输出上限", status_code=502)
    return json.loads(result["stdout"])


async def developer_inputs(
    service, machine_id: str, thread_id: str, *, preview: bool = False
) -> dict:
    await service.message_history.ready(machine_id, thread_id)
    thread = (await service.read_thread(machine_id, thread_id))["thread"]
    path, end = thread.get("path"), None
    fresh = service.sessions.fresh_threads.get((machine_id, thread_id))
    if fresh is not None and not fresh.get("turns"):
        path = None
    histories = []
    while path:
        path, data = await service.message_history.read(machine_id, path, end)
        records = [json.loads(line) for line in data.splitlines()]
        histories.append(records)
        boundary = records[0]["payload"].get("history_base")
        if not boundary:
            break
        path = await service.message_history.ancestor_path(machine_id, path, boundary["thread_id"])
        end = boundary["end_byte_offset"]

    base = ""
    context = service.tool_context.thread(machine_id, thread_id)
    configured = json.loads(context["meta"])["instructions"] if context else ""
    custom = configured
    messages = []
    catalog = {}
    pending_update = None
    saved = False

    def add_item(item):
        nonlocal base, custom, saved, pending_update
        if item.get("type") == "additional_tools":
            updates = update_tools(item["tools"], catalog)
            if pending_update is not None:
                pending_update["toolUpdates"] = updates
            pending_update = None
            return
        pending_update = None
        if item.get("type") != "message" or item.get("role") != "developer":
            return
        saved = True
        kinds = (item.get("internal_chat_message_metadata_passthrough") or {}).get(
            "content_item_kinds", []
        )
        runtime = []
        for index, part in enumerate(item.get("content", [])):
            if part.get("type") != "input_text":
                continue
            text = part["text"]
            kind = kinds[index] if index < len(kinds) else ""
            if kind == "model.base_instructions":
                base = text
            elif kind == "generic.developer_instructions" or (custom and text == custom):
                custom = text
            elif text and text != base:
                runtime.append(text)
        if runtime:
            messages.append(
                {
                    "id": item.get("id") or f"developer-{len(messages) + 1}",
                    "text": "\n".join(runtime),
                }
            )
            if "tools.incremental_update" in kinds:
                pending_update = messages[-1]

    for records in reversed(histories):
        for record in records:
            payload = record.get("payload", {})
            if record["type"] == "session_meta":
                instructions = payload.get("base_instructions") or {}
                base = (
                    instructions if isinstance(instructions, str) else instructions.get("text", "")
                )
            elif record["type"] == "compacted" and payload.get("replacement_history") is not None:
                saved = True
                custom = configured
                messages.clear()
                catalog.clear()
                pending_update = None
                for item in payload["replacement_history"]:
                    add_item(item)
            elif record["type"] == "response_item":
                add_item(payload)
    source = "saved"
    if preview and not saved and not service.demo:
        source = "preview"
        for item in await preview_inputs(service, machine_id, thread_id):
            add_item(item)
    return {
        "baseInstructions": base,
        "runtimeMessages": messages,
        "customInstructions": custom,
        "source": source,
    }
