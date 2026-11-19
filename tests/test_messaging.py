import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from nexus.core import SessionService
from nexus.errors import NexusError
from nexus.store import Store

SENDER = "01900000-0000-7000-8000-000000a1b2c3"
FIRST = "01900000-0000-7000-8000-000000b2c3d4"
SECOND = "01900000-0000-7000-8000-000000c3d4e5"


class MessagingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = SessionService(Path(self.directory.name))
        self.service.machine("local")["projects"] = ["/project"]
        self.other = await self.service.add_machine({"name": "other", "transport": "local"})
        self.other["projects"] = ["/project"]
        self.service.machine(self.other["id"])["projects"] = ["/project"]
        self.service.store.save()
        self.service.search_index.remember(
            "local",
            [
                {"id": SENDER, "cwd": "/project"},
                {"id": FIRST, "cwd": "/project"},
            ],
        )
        self.service.search_index.remember(self.other["id"], [{"id": SECOND, "cwd": "/project"}])
        self.messaging = self.service.messaging

    async def asyncTearDown(self):
        await self.service.close()
        self.directory.cleanup()

    async def test_resolves_cached_offline_and_new_unindexed_sessions(self):
        result = await self.messaging.resolve("C3D4E5")
        self.assertEqual(
            result, {"botId": "c3d4e5", "machineId": self.other["id"], "threadId": SECOND}
        )
        fresh = "01900000-0000-7000-8000-000000d4e5f6"
        self.service.sessions.remember_thread("local", {"id": fresh, "cwd": "/project"})
        self.assertEqual((await self.messaging.resolve("d4e5f6"))["threadId"], fresh)

    async def test_ambiguous_short_id_requires_a_full_id_or_machine(self):
        duplicate = "01900001-0000-7000-8000-000000b2c3d4"
        self.service.search_index.remember(self.other["id"], [{"id": duplicate, "cwd": "/project"}])
        with self.assertRaises(NexusError) as raised:
            await self.messaging.resolve("b2c3d4")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(len(raised.exception.data["candidates"]), 2)
        self.assertEqual((await self.messaging.resolve("b2c3d4", "local"))["threadId"], FIRST)
        self.assertEqual((await self.messaging.resolve(duplicate))["machineId"], self.other["id"])

    async def test_cc_headers_are_specific_to_each_deduplicated_recipient(self):
        with patch.object(
            self.service,
            "send_message",
            new_callable=AsyncMock,
            return_value={"turnId": "turn", "mode": "start"},
        ) as send:
            result = await self.messaging.send(
                {
                    "senderThreadId": SENDER,
                    "threadId": ["b2c3d4", "c3d4e5", FIRST],
                    "cc": True,
                    "text": "请检查",
                }
            )
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["data"]), 2)
        messages = {call.args[1]: call.args[2] for call in send.await_args_list}
        self.assertEqual(
            messages[FIRST]["input"],
            [{"type": "text", "text": "From [a1b2c3], CC [c3d4e5]: 请检查"}],
        )
        self.assertEqual(
            messages[SECOND]["input"],
            [{"type": "text", "text": "From [a1b2c3], CC [b2c3d4]: 请检查"}],
        )
        self.assertEqual(messages[FIRST]["actorName"], "a1b2c3")
        self.assertEqual(
            messages[FIRST]["delivery"],
            {
                "senderBotId": "a1b2c3",
                "groupName": None,
                "cc": ["c3d4e5"],
                "promptPrefix": "From [a1b2c3], CC [c3d4e5]: ",
            },
        )

    async def test_plain_broadcast_keeps_the_same_header_and_reports_delivery_failures(self):
        async def deliver(machine, thread, message):
            self.assertEqual(message["input"][0]["text"], "From [a1b2c3]: hello")
            if thread == SECOND:
                raise NexusError("机器未连接", status_code=503)
            return {"turnId": "turn", "mode": "steer"}

        with patch.object(self.service, "send_message", side_effect=deliver):
            result = await self.messaging.send(
                {"senderThreadId": SENDER, "threadId": ["b2c3d4", "c3d4e5"], "text": "hello"}
            )
        self.assertEqual(result["data"][0]["mode"], "steer")
        self.assertEqual(result["errors"][0]["threadId"], SECOND)

    async def test_input_preserves_images_skills_and_the_callers_items(self):
        items = [
            {"type": "localImage", "path": "/image.png"},
            {"type": "skill", "name": "review", "path": "/review/SKILL.md"},
        ]
        with patch.object(
            self.service,
            "send_message",
            new_callable=AsyncMock,
            return_value={"turnId": "turn", "mode": "start"},
        ) as send:
            await self.messaging.send(
                {"senderThreadId": SENDER, "threadId": "b2c3d4", "input": items}
            )
        delivered = send.await_args.args[2]["input"]
        self.assertEqual(delivered, [{"type": "text", "text": "From [a1b2c3]: "}, *items])
        self.assertEqual(items[0]["type"], "localImage")

    async def test_group_members_persist_and_sender_is_not_sent_its_own_message(self):
        await self.messaging.set_group("开发组", ["a1b2c3", "b2c3d4", "c3d4e5"])
        saved = Store(Path(self.directory.name)).data["groups"]["开发组"]
        self.assertEqual(saved[1], {"machineId": "local", "threadId": FIRST})
        with patch.object(
            self.service,
            "send_message",
            new_callable=AsyncMock,
            return_value={"turnId": "turn", "mode": "start"},
        ) as send:
            result = await self.messaging.send(
                {"senderThreadId": SENDER, "groupName": "开发组", "text": "同步"}
            )
        self.assertEqual([item["threadId"] for item in result["data"]], [FIRST, SECOND])
        for call in send.await_args_list:
            self.assertEqual(
                call.args[2]["input"][0]["text"], "In group [开发组], From [a1b2c3]: 同步"
            )
        await self.messaging.delete_group("开发组")
        self.assertEqual(Store(Path(self.directory.name)).data["groups"], {})

    async def test_all_recipients_resolve_before_any_message_is_sent(self):
        with (
            patch.object(self.service, "list_threads", new_callable=AsyncMock),
            patch.object(self.service, "send_message", new_callable=AsyncMock) as send,
        ):
            with self.assertRaises(NexusError):
                await self.messaging.send(
                    {"senderThreadId": SENDER, "threadId": ["b2c3d4", "ffffff"], "text": "hello"}
                )
        send.assert_not_awaited()

    async def test_user_group_message_is_recorded_once_and_uses_user_prompt(self):
        await self.messaging.set_group("协作", ["a1b2c3", "b2c3d4"])
        body = {
            "senderKind": "human",
            "groupName": "协作",
            "text": "进展如何？",
            "clientMessageId": "user-message",
        }
        with patch.object(
            self.service,
            "send_message",
            new_callable=AsyncMock,
            return_value={"turnId": "turn", "mode": "start"},
        ) as send:
            first = await self.messaging.send(body)
            repeated = await self.messaging.send(body)
        self.assertEqual(send.await_count, 2)
        self.assertEqual(first["message"]["id"], repeated["message"]["id"])
        self.assertEqual(first["message"]["status"], "sent")
        for call in send.await_args_list:
            self.assertEqual(
                call.args[2]["input"][0]["text"], "In group [协作], From user: 进展如何？"
            )
            self.assertEqual(call.args[2]["actor"], "human")
        history = self.messaging.history("协作")["data"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["senderKind"], "human")
        self.assertEqual(history[0]["text"], "进展如何？")
        self.assertTrue(history[0]["createdAt"])

    async def test_group_log_retains_partial_delivery_and_survives_database_reopen(self):
        await self.messaging.set_group("协作", ["b2c3d4", "c3d4e5"])

        async def deliver(machine, thread, message):
            if thread == SECOND:
                raise NexusError("机器未连接", status_code=503)
            return {"turnId": "turn", "mode": "steer"}

        with patch.object(self.service, "send_message", side_effect=deliver):
            result = await self.messaging.send(
                {"senderThreadId": SENDER, "groupName": "协作", "text": "通知"}
            )
        self.assertEqual(result["message"]["status"], "partial")
        self.assertEqual(result["message"]["senderBotId"], "a1b2c3")
        reopened = SessionService(Path(self.directory.name))
        try:
            saved = reopened.messaging.history("协作")["data"][0]
            self.assertEqual(saved, result["message"])
            events = [
                event
                for event in self.service.events.history
                if event["method"] == "nexus/group/message"
            ]
            self.assertEqual(
                [event["params"]["message"]["status"] for event in events], ["pending", "partial"]
            )
        finally:
            await reopened.close()
