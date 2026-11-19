"""Bot addresses, named recipient sets, and agent message delivery."""

import asyncio
import re
from copy import deepcopy
from uuid import UUID

from .errors import NexusError
from .group_messages import GroupMessages
from .session_state import is_subagent
from .sides import is_side


def bot_id(thread_id: str) -> str:
    return thread_id[-6:].lower()


class AgentMessaging:
    def __init__(self, service):
        self.service = service
        self.messages = GroupMessages(service.database)

    @staticmethod
    def address(machine_id: str, thread_id: str) -> dict:
        return {"botId": bot_id(thread_id), "machineId": machine_id, "threadId": thread_id}

    async def resolve(self, identifier: str, machine_id: str | None = None) -> dict:
        identifier = identifier.strip().lower()
        short = bool(re.fullmatch(r"[0-9a-f]{6}", identifier))
        if not short:
            try:
                identifier = str(UUID(identifier))
            except ValueError:
                raise NexusError("请提供六位 bot ID 或完整会话 ID") from None
        service = self.service
        if machine_id:
            service.machine(machine_id)
        projects = {
            (machine["id"], cwd)
            for machine in service._configurations()
            for cwd in machine.get("projects", [])
        }

        async def matches() -> dict:
            saved = await asyncio.to_thread(
                service.search_index.find_addresses, identifier, machine_id
            )
            threads = {(thread["machineId"], thread["id"]): thread for thread in saved}
            threads.update(
                {
                    key: thread
                    for key, thread in service.sessions.thread_details.items()
                    if (not machine_id or key[0] == machine_id)
                    and (key[1] == identifier or short and bot_id(key[1]) == identifier)
                }
            )
            return {
                key: thread
                for key, thread in threads.items()
                if (key[0], thread.get("cwd")) in projects and not thread.get("ephemeral")
            }

        found = await matches()
        if not found and machine_id and not short:
            await service.read_thread(machine_id, identifier)
            return self.address(machine_id, identifier)
        if not found:
            await service.list_threads(machine_id, {"scope": "loaded"})
            found = await matches()
        if not found:
            raise NexusError(f"找不到 bot {identifier}", status_code=404)
        if len(found) > 1:
            candidates = [self.address(*key) for key in sorted(found)]
            names = ", ".join(f"{item['machineId']}/{item['threadId']}" for item in candidates)
            raise NexusError(
                f"ID {identifier} 对应多个会话，请指定机器或完整 ID：{names}",
                status_code=409,
                data={"candidates": candidates},
            )
        return self.address(*next(iter(found)))

    async def recipients(self, identifiers: list[str], machine_id: str | None = None) -> list[dict]:
        if not identifiers:
            raise NexusError("请至少指定一个收件人")
        resolved = await asyncio.gather(*(self.resolve(value, machine_id) for value in identifiers))
        return list({(item["machineId"], item["threadId"]): item for item in resolved}.values())

    def groups(self) -> dict:
        return {
            name: [self.address(item["machineId"], item["threadId"]) for item in members]
            for name, members in self.service.store.data.get("groups", {}).items()
        }

    async def bots(self) -> list[dict]:
        saved = await asyncio.to_thread(self.service.search_index.all_addresses)
        threads = {(item["machineId"], item["id"]): item for item in saved}
        for key, thread in self.service.sessions.thread_details.items():
            threads[key] = {**threads.get(key, {}), **thread}
        machines = {item["id"]: item for item in self.service._configurations()}
        return [
            {
                **self.address(host, thread_id),
                "name": thread.get("name") or thread.get("preview", "").split("\n")[0][:80],
                "machineName": machines[host]["name"],
                "cwd": thread.get("cwd", ""),
                **{
                    field: thread[field]
                    for field in ("status", "recencyAt", "updatedAt", "createdAt")
                    if field in thread
                },
            }
            for (host, thread_id), thread in threads.items()
            if host in machines
            and thread.get("cwd") in machines[host].get("projects", [])
            and not thread.get("ephemeral")
            and not thread.get("archived")
            and not is_side(thread)
            and not is_subagent(thread)
            and (host, thread_id) not in self.service.sides.saved
        ]

    async def check_members(self, members: list[dict]) -> None:
        eligible = {(item["machineId"], item["threadId"]) for item in await self.bots()}
        if any((item["machineId"], item["threadId"]) not in eligible for item in members):
            raise NexusError("子代理、侧对话或已归档的会话不能加入群聊")

    def history(self, name, before=None, limit=50):
        if name not in self.groups():
            raise NexusError("找不到这个群", status_code=404)
        return self.messages.history(name, before, limit)

    async def set_group(self, name: str, identifiers: list[str]) -> dict:
        if not name.strip():
            raise NexusError("请填写群名")
        existing = self.service.store.data.get("groups", {}).get(name, [])
        if not identifiers and name not in self.service.store.data.get("groups", {}):
            raise NexusError("请选择群成员")
        members = await self.recipients(identifiers) if identifiers else []
        await self.check_members(
            [
                item
                for item in members
                if {"machineId": item["machineId"], "threadId": item["threadId"]} not in existing
            ]
        )
        self.service.store.data.setdefault("groups", {})[name] = [
            {"machineId": item["machineId"], "threadId": item["threadId"]} for item in members
        ]
        self.service.store.save()
        await self.service.events.publish(
            "", "nexus/group/updated", {"groupName": name, "members": members}
        )
        return {"name": name, "members": members}

    async def set_thread_groups(self, machine_id: str, thread_id: str, names: list[str]) -> dict:
        self.service.machine(machine_id)
        groups = self.service.store.data.setdefault("groups", {})
        selected = set(names)
        if selected - groups.keys():
            raise NexusError("找不到这个群", status_code=404)
        member = {"machineId": machine_id, "threadId": thread_id}
        if any(member not in groups[name] for name in selected):
            await self.check_members([member])
        changed = {}
        for name, members in groups.items():
            included = member in members
            if included == (name in selected):
                continue
            groups[name] = (
                [*members, member]
                if name in selected
                else [item for item in members if item != member]
            )
            changed[name] = [
                self.address(item["machineId"], item["threadId"]) for item in groups[name]
            ]
        if changed:
            self.service.store.save()
        for name, members in changed.items():
            await self.service.events.publish(
                "", "nexus/group/updated", {"groupName": name, "members": members}
            )
        return {"groups": self.groups()}

    async def delete_group(self, name: str) -> dict:
        groups = self.service.store.data.get("groups", {})
        if name not in groups:
            raise NexusError("找不到这个群", status_code=404)
        self.messages.delete(name)
        del groups[name]
        self.service.store.save()
        await self.service.events.publish("", "nexus/group/deleted", {"groupName": name})
        return {"deleted": True}

    async def send(self, body: dict) -> dict:
        sender_thread = body.get("senderThreadId")
        human = body.get("senderKind") == "human"
        if not sender_thread and not human:
            raise NexusError("缺少发送者会话 ID")
        sender = None if human else bot_id(sender_thread)
        target = body.get("threadId")
        group = body.get("groupName")
        cc = body.get("cc", False)
        record = None
        if (target is None) == (group is None):
            raise NexusError("请指定 threadId 或 groupName 其中一项")
        if group is not None:
            if cc:
                raise NexusError("群消息直接使用群名，无需设置抄送")
            groups = self.groups()
            if group not in groups:
                raise NexusError("找不到这个群", status_code=404)
            recipients = [item for item in groups[group] if item["threadId"] != sender_thread]
            text = (
                "\n".join(item["text"] for item in body["input"] if item["type"] == "text")
                if body.get("input") is not None
                else body.get("text", "")
            )
            record, created = self.messages.create(
                group,
                "human" if human else "agent",
                sender,
                sender_thread,
                text,
                body.get("clientMessageId"),
            )
            if not created:
                return {
                    "senderBotId": sender,
                    **(record["delivery"] or {"data": [], "errors": []}),
                    "message": record,
                }
            await self.service.events.publish(
                "", "nexus/group/message", {"groupName": group, "message": record}
            )
        else:
            recipients = await self.recipients(
                [target] if isinstance(target, str) else target, body.get("machineId")
            )

        async def deliver(recipient: dict) -> dict:
            header = "From user" if human else f"From [{sender}]"
            copied = []
            if group is not None:
                header = f"In group [{group}], {header}"
            elif cc:
                copied = [item["botId"] for item in recipients if item != recipient]
                if copied:
                    header += ", CC " + ", ".join(f"[{identifier}]" for identifier in copied)
            header += ": "
            items = deepcopy(
                body["input"]
                if body.get("input") is not None
                else [
                    {"type": "text", "text": body.get("text", "")},
                ]
            )
            first_text = next((item for item in items if item["type"] == "text"), None)
            if first_text is None:
                items.insert(0, {"type": "text", "text": header})
            else:
                first_text["text"] = header + first_text["text"]
            message = {
                key: body[key]
                for key in (
                    "model",
                    "effort",
                    "serviceTier",
                    "clientUserMessageId",
                )
                if key in body
            }
            message.update(
                input=items,
                actor="human" if human else "agent",
                actorName=body.get("actorName") or sender,
                delivery={
                    "senderBotId": sender,
                    "groupName": group,
                    "cc": copied,
                    "promptPrefix": header,
                },
            )
            result = await self.service.send_message(
                recipient["machineId"], recipient["threadId"], message
            )
            return {**recipient, **result}

        if isinstance(target, str):
            return {"senderBotId": sender, **await deliver(recipients[0])}
        results = await asyncio.gather(
            *(deliver(item) for item in recipients), return_exceptions=True
        )
        data, errors = [], []
        for recipient, result in zip(recipients, results):
            if isinstance(result, Exception):
                errors.append({**recipient, "error": str(result)})
            else:
                data.append(result)
        result = {"senderBotId": sender, "data": data, "errors": errors}
        if record:
            record = self.messages.finish(record["id"], {"data": data, "errors": errors})
            await self.service.events.publish(
                "", "nexus/group/message", {"groupName": group, "message": record}
            )
            result["message"] = record
        return result
