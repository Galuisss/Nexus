import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from nexus.app import create_app
from nexus.core import NexusError, SessionService
from nexus.store import Store


class EventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = SessionService(Path(self.directory.name))
        self.service.loop = asyncio.get_running_loop()
        self.service.worker = asyncio.create_task(self.service._dispatch())
        watch = patch.object(self.service.history_watch, "start")
        watch.start()
        self.addCleanup(watch.stop)

    async def asyncTearDown(self):
        await self.service.close()
        self.directory.cleanup()

    async def consumed(self, source):
        self.service._enqueue("notification", "local", source, ("check/end", {}))
        async with asyncio.timeout(2):
            while not any(event["method"] == "check/end" for event in self.service.events.history):
                await asyncio.sleep(0)

    async def test_stop_goal_pauses_before_interrupting_the_current_turn(self):
        order = []

        async def rpc(machine, method, params):
            order.append(method)
            self.assertEqual(params["origin"], "user")
            return {"goal": {"status": "paused"}}

        async def active(machine, thread):
            order.append("active-turn")
            return "turn"

        async def interrupt(machine, thread, turn):
            order.append("interrupt")

        with (
            patch.object(self.service, "rpc", side_effect=rpc),
            patch.object(self.service, "active_turn_id", side_effect=active),
            patch.object(self.service, "interrupt", side_effect=interrupt),
        ):
            result = await self.service.stop_goal("local", "thread")
        self.assertEqual(order, ["thread/goal/set", "active-turn", "interrupt"])
        self.assertEqual(result["goal"]["status"], "paused")

    async def test_goal_update_preserves_null_budget_and_mutation_origin(self):
        for origin in ("user", "automatic"):
            with (
                patch.object(self.service, "thread_state", return_value={"thread": {}}),
                patch.object(self.service, "rpc", return_value={"goal": {}}) as rpc,
            ):
                await self.service.update_goal(
                    "local", "thread", {"status": "active", "tokenBudget": None, "origin": origin}
                )
            rpc.assert_awaited_once_with(
                "local", "thread/goal/set",
                {"threadId": "thread", "status": "active", "tokenBudget": None, "origin": origin},
            )

    async def test_goal_events_and_snapshots_keep_native_usage(self):
        state = self.service.sessions
        state.thread_details[("local", "thread")] = {"id": "thread"}
        goal = {"objective": "optimize", "status": "blocked", "tokensUsed": 5553}
        state.apply("local", "thread/goal/updated", {"threadId": "thread", "goal": goal})
        snapshot = await self.service._snapshot_result("local", "thread", {"data": []})
        self.assertEqual(snapshot["goal"], goal)
        self.assertTrue(snapshot["goalSupported"])
        state.apply("local", "thread/goal/cleared", {"threadId": "thread"})
        self.assertIsNone(state.goals[("local", "thread")])
        self.assertEqual(snapshot["goal"]["tokensUsed"], 5553)

    async def test_machine_metadata_and_equivalent_settings_preserve_connection(self):
        service = self.service
        machine = service.machine("local")
        connection = SimpleNamespace(close=AsyncMock())
        service.runtime.connections["local"] = connection
        service.runtime.status["local"] = {"status": "connected"}
        service.runtime.wanted.add("local")
        service.sessions.active_turns[("local", "thread")] = "turn"
        for settings in (
            {},
            {"name": "renamed"},
            {"id": "ignored"},
            {"codexBin": "codex", "connection": "daemon", "env": {}},
            {"host": "unused", "user": "unused", "port": 2222},
        ):
            with self.subTest(settings=settings):
                result = await service.update_machine("local", settings)
                self.assertEqual(result["status"], "connected")
                self.assertEqual(result["id"], "local")
        machine.update(transport="ssh", host="host", user=None, port=None, codexBin="")
        result = await service.update_machine(
            "local", {"user": "", "port": 22, "codexBin": "codex"}
        )
        self.assertEqual(result["status"], "connected")
        result = await service.update_machine("local", {"port": "22"})
        self.assertEqual(result["status"], "connected")
        self.assertIs(service.runtime.connections["local"], connection)
        self.assertIn("local", service.runtime.wanted)
        self.assertEqual(service.sessions.active_turns[("local", "thread")], "turn")
        connection.close.assert_not_awaited()
        self.assertEqual(Store(Path(self.directory.name)).machines[0]["name"], "renamed")

    async def test_changed_runtime_settings_use_existing_disconnect(self):
        service = self.service
        baseline = {
            "transport": "ssh",
            "host": "host",
            "user": "user",
            "port": 22,
            "codexBin": "codex",
            "connection": "daemon",
            "env": {},
        }
        for key, value in (
            ("transport", "local"),
            ("host", "other"),
            ("user", "other"),
            ("port", 2222),
            ("codexBin", "/opt/codex"),
            ("connection", "spawn"),
            ("env", {"CODEX_HOME": "/tmp/codex"}),
        ):
            with self.subTest(setting=key):
                service.machine("local").update(baseline)
                connection = SimpleNamespace(close=AsyncMock())
                service.runtime.connections["local"] = connection
                service.runtime.status["local"] = {"status": "connected"}
                service.runtime.wanted.add("local")
                result = await service.update_machine("local", {key: value})
                self.assertEqual(result["status"], "disconnected")
                self.assertEqual(result[key], value)
                connection.close.assert_awaited_once()
                self.assertNotIn("local", service.runtime.connections)
                self.assertNotIn("local", service.runtime.wanted)
                self.assertEqual(Store(Path(self.directory.name)).machines[0][key], value)

    async def test_project_list_replacement_persists_and_updates_search_without_disconnecting(self):
        service = self.service
        new_project = str(Path(self.directory.name) / "new")
        Path(new_project).mkdir()
        machine = service.machine("local")
        machine.update(projects=["/old", "/keep"], projectActivity={"/old": 10, "/keep": 20})
        connection = SimpleNamespace(close=AsyncMock())
        service.runtime.connections["local"] = connection
        service.runtime.status["local"] = {"status": "connected"}
        with patch.object(service.search_sync, "wake") as wake:
            result = await service.update_machine("local", {"projects": ["/keep", new_project]})
            self.assertEqual(result["projects"], ["/keep", new_project])
            self.assertEqual(result["projectActivity"], {"/keep": 20})
            self.assertEqual(result["status"], "connected")
            self.assertEqual(Store(Path(self.directory.name)).machines[0], machine)
            event = service.events.history[-1]
            self.assertEqual(event["method"], "nexus/machine/updated")
            self.assertEqual(event["params"]["machine"], result)
            wake.assert_called_once_with("local")
            await service.update_machine("local", {"projects": ["/keep", new_project]})
            wake.assert_called_once_with("local")
        connection.close.assert_not_awaited()

    async def test_stale_connection_notifications_and_requests_are_discarded(self):
        old = SimpleNamespace(close=AsyncMock())
        current = SimpleNamespace(close=AsyncMock())
        self.service.runtime.connections["local"] = current
        self.service._enqueue(
            "notification",
            "local",
            old,
            ("turn/started", {"threadId": "thread", "turn": {"id": "old"}}),
        )
        self.service._enqueue(
            "request",
            "local",
            old,
            {"id": 1, "method": "approval", "params": {"threadId": "thread"}},
        )
        await self.consumed(current)
        self.assertFalse(self.service.sessions.active_turns)
        self.assertFalse(self.service.pending_requests)
        self.assertEqual(self.service.health()["status"], "ok")

    async def test_deleted_machine_completion_does_not_stop_other_events(self):
        old = SimpleNamespace(close=AsyncMock())
        self.service.runtime.connections["local"] = old
        await self.service.remove_machine("local")
        self.service._enqueue(
            "notification",
            "local",
            old,
            (
                "turn/completed",
                {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}},
            ),
        )
        other = await self.service.add_machine({"transport": "local", "name": "other"})
        source = SimpleNamespace(close=AsyncMock())
        self.service.runtime.connections[other["id"]] = source
        self.service._enqueue(
            "notification",
            other["id"],
            source,
            ("turn/started", {"threadId": "thread", "turn": {"id": "new"}}),
        )
        async with asyncio.timeout(2):
            while not self.service.sessions.active_turns:
                await asyncio.sleep(0)
        self.assertEqual(self.service.sessions.active_turns[(other["id"], "thread")], "new")
        self.assertEqual(self.service.health()["status"], "ok")

    async def test_handler_error_publishes_resync_and_continues(self):
        source = SimpleNamespace(close=AsyncMock())
        self.service.runtime.connections["local"] = source
        with self.assertLogs("nexus.core", level="ERROR"):
            self.service._enqueue("notification", "local", source, ("item/started", {}))
            await self.consumed(source)
        self.assertIn("nexus/resync", [event["method"] for event in self.service.events.history])
        self.assertEqual(self.service.health()["status"], "ok")

    async def test_delete_clears_search_work_without_scheduling_another_read(self):
        service = self.service
        thread = {"id": "deleted", "cwd": "/project", "name": "side", "threadSource": "nexus-side"}
        key = ("local", thread["id"])
        for notify in (True, False):
            with self.subTest(native_notification=notify):

                async def rpc(method, params, *, on_response):
                    self.assertEqual(method, "thread/delete")
                    if notify:
                        service._enqueue(
                            "notification", "local", source, ("thread/deleted", params)
                        )
                    on_response({})
                    return {}

                source = SimpleNamespace(rpc=rpc, close=AsyncMock())
                service.runtime.connections["local"] = source
                service.runtime.status["local"] = {"status": "connected"}
                service.sessions.remember_thread("local", thread)
                service.search_index.remember("local", [thread])
                for pending in (
                    service.search_sync.dirty,
                    service.search_sync.content,
                    service.search_sync.rebuild,
                    service.search_sync.external,
                ):
                    pending["local"].add(thread["id"])

                with patch.object(service.search_sync, "wake") as wake:
                    await service.rpc("local", "thread/delete", {"threadId": thread["id"]})
                    wake.assert_not_called()

                self.assertNotIn(key, service.sessions.thread_details)
                self.assertEqual(
                    service.search_index.query("side", "title", [("local", "/project")])["data"], []
                )
                for pending in (
                    service.search_sync.dirty,
                    service.search_sync.content,
                    service.search_sync.rebuild,
                    service.search_sync.external,
                ):
                    self.assertNotIn(thread["id"], pending["local"])

    async def test_reconnect_restores_observers_and_manual_disconnect_cancels_retry(self):
        service = self.service
        connection = SimpleNamespace(connect=AsyncMock(), close=AsyncMock(), version="0.169.0")
        with (
            patch("nexus.connections.RuntimeConnection", return_value=connection),
            patch.object(service, "_restore_observers", new_callable=AsyncMock) as restore,
            patch.object(service, "_start_project_index"),
            patch.object(service.search_sync, "wake"),
        ):
            await service.connect_machine("local")
            service.sessions.active_turns[("local", "thread")] = "turn"
            service.sessions.resume_threads.add(("local", "thread"))
            await service.runtime.disconnected("local", connection, "lost")
            self.assertEqual(service.sessions.recovering_turns[("local", "thread")], "turn")
            self.assertIn(("local", "thread"), service.sessions.resume_threads)
            await service.connect_machine("local")
            self.assertEqual(restore.await_count, 2)
            await service.disconnect_machine("local")
            self.assertNotIn("local", service.runtime.reconnect_tasks)
            self.assertNotIn("local", service.runtime.wanted)
            self.assertEqual(service.runtime.status["local"]["status"], "disconnected")

    async def test_manual_reconnect_replaces_connection_and_preserves_turn_recovery(self):
        service = self.service
        old, new = [
            SimpleNamespace(connect=AsyncMock(), close=AsyncMock(), version="0.169.0")
            for _ in range(2)
        ]
        key = ("local", "thread")
        with (
            patch("nexus.connections.RuntimeConnection", side_effect=[old, new]),
            patch.object(service, "_restore_observers", new_callable=AsyncMock) as restore,
            patch.object(service, "_start_project_index"),
            patch.object(service.search_sync, "wake"),
        ):
            await service.connect_machine("local")
            service.sessions.active_turns[key] = "turn"
            service.sessions.resume_threads.add(key)
            service.sessions.observed_threads.add(key)

            result = await service.reconnect_machine("local")

            self.assertEqual(result["status"], "connected")
            self.assertIs(service.runtime.connections["local"], new)
            old.close.assert_awaited_once()
            new.connect.assert_awaited_once()
            restore.assert_awaited_with("local", new)
            self.assertEqual(service.sessions.recovering_turns[key], "turn")
            self.assertIn(key, service.sessions.resume_threads)
            self.assertNotIn(key, service.sessions.observed_threads)
            self.assertIn("local", service.runtime.wanted)

    async def test_manual_reconnect_cancels_backoff_and_retries_after_failure(self):
        service = self.service
        failed = SimpleNamespace(
            connect=AsyncMock(side_effect=RuntimeError("offline")), close=AsyncMock()
        )
        with patch("nexus.connections.RuntimeConnection", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                await service.connect_machine("local")
            retry = service.runtime.reconnect_tasks["local"]
            key = ("local", "thread")
            service.sessions.recovering_turns[key] = "turn"

            with self.assertRaisesRegex(RuntimeError, "offline"):
                await service.reconnect_machine("local")

            self.assertTrue(retry.cancelled())
            self.assertIsNot(service.runtime.reconnect_tasks["local"], retry)
            self.assertEqual(failed.connect.await_count, 2)
            self.assertEqual(service.runtime.status["local"]["status"], "reconnecting")
            self.assertEqual(service.sessions.recovering_turns[key], "turn")
            self.assertIn("local", service.runtime.wanted)

    async def test_connecting_machine_lists_use_cached_sessions_without_waiting_for_connection(
        self,
    ):
        service = self.service
        service.machine("local")["projects"] = ["/project"]
        cached = {"data": [{"id": "thread", "cwd": "/project"}], "nextCursor": None}
        service.listed_threads["local"] = cached
        with patch.object(service, "rpc", new_callable=AsyncMock) as rpc:
            for status in ("connecting", "reconnecting"):
                with self.subTest(status=status):
                    service.runtime.status["local"] = {"status": status}
                    self.assertEqual(await service.list_threads("local"), cached)
                    with self.assertRaises(NexusError) as error:
                        await service.list_threads("local", {"scope": "history"})
                    self.assertEqual(error.exception.status_code, 502)
            rpc.assert_not_awaited()

    async def test_concurrent_connects_share_one_attempt_and_cancelled_caller_does_not_stop_it(
        self,
    ):
        started, release = asyncio.Event(), asyncio.Event()

        async def fail():
            started.set()
            await release.wait()
            raise RuntimeError("offline")

        connection = SimpleNamespace(connect=AsyncMock(side_effect=fail), close=AsyncMock())
        with patch("nexus.connections.RuntimeConnection", return_value=connection) as factory:
            callers = [asyncio.create_task(self.service.connect_machine("local")) for _ in range(3)]
            try:
                await started.wait()
                callers[0].cancel()
                await asyncio.gather(callers[0], return_exceptions=True)
                self.assertFalse(self.service.runtime.connect_tasks["local"].done())
            finally:
                release.set()
                results = await asyncio.gather(*callers, return_exceptions=True)
            self.assertTrue(all(isinstance(result, RuntimeError) for result in results[1:]))
            factory.assert_called_once()
            connection.close.assert_awaited_once()

    async def test_connection_deadline_releases_lock_and_schedules_retry(self):
        connection = SimpleNamespace(
            connect=AsyncMock(side_effect=asyncio.Event().wait), close=AsyncMock()
        )
        with (
            patch("nexus.connections.RuntimeConnection", return_value=connection),
            patch("nexus.connections.CONNECT_TIMEOUT", 0.02),
        ):
            with self.assertRaises(NexusError) as error:
                await self.service.connect_machine("local")
        self.assertEqual(error.exception.status_code, 504)
        self.assertFalse(self.service.runtime.locks["local"].locked())
        self.assertNotIn("local", self.service.runtime.connections)
        self.assertIn("local", self.service.runtime.reconnect_tasks)
        self.assertEqual(self.service.runtime.status["local"]["error"], "连接机器超时")
        connection.close.assert_awaited_once()

    async def test_offline_rpc_fails_without_starting_a_connection(self):
        with patch.object(self.service, "connect_machine", new_callable=AsyncMock) as connect:
            for status in ("disconnected", "connecting", "reconnecting"):
                self.service.runtime.status["local"] = {"status": status}
                with self.assertRaises(NexusError) as error:
                    await self.service.rpc("local", "thread/loaded/list", {})
                self.assertEqual(error.exception.status_code, 503)
            connect.assert_not_awaited()

    async def test_disconnect_cancels_an_in_progress_connection(self):
        started = asyncio.Event()

        async def stall():
            started.set()
            await asyncio.Event().wait()

        connection = SimpleNamespace(connect=AsyncMock(side_effect=stall), close=AsyncMock())
        with patch("nexus.connections.RuntimeConnection", return_value=connection):
            caller = asyncio.create_task(self.service.connect_machine("local"))
            await started.wait()
            await self.service.disconnect_machine("local")
            with self.assertRaises(asyncio.CancelledError):
                await caller
        connection.close.assert_awaited_once()
        self.assertNotIn("local", self.service.runtime.connect_tasks)
        self.assertNotIn("local", self.service.runtime.reconnect_tasks)
        self.assertEqual(self.service.runtime.status["local"]["status"], "disconnected")

    async def test_session_list_has_a_deadline_for_a_connected_but_unresponsive_machine(self):
        async def stall(*args, **kwargs):
            await asyncio.Event().wait()

        self.service.machine("local")["projects"] = ["/project"]
        self.service.runtime.status["local"] = {"status": "connected"}
        connection = SimpleNamespace(rpc=AsyncMock(side_effect=stall), close=AsyncMock())
        self.service.runtime.connections["local"] = connection
        with patch("nexus.core.LIST_TIMEOUT", 0.02):
            with self.assertRaises(NexusError) as error:
                await self.service.list_threads("local")
        self.assertEqual(error.exception.status_code, 504)

    async def test_history_archive_filters_and_merged_pagination_include_unloaded_threads(self):
        self.service.machine("local")["projects"] = ["/project", "/second"]
        self.service.runtime.status["local"] = {"status": "connected"}
        rows = [
            {
                "id": str(index),
                "name": "topic",
                "preview": "saved",
                "cwd": "/second" if index % 3 == 0 else "/project",
                "updatedAt": 100 - index // 2,
                "archived": index % 3 == 1,
                "status": {"type": "notLoaded"},
            }
            for index in range(17)
        ]
        rows.extend(
            [
                {**rows[0], "id": "outside", "cwd": "/outside", "updatedAt": 200},
                {**rows[0], "id": "other-topic", "name": "other", "updatedAt": 199},
                {**rows[0], "id": "empty", "preview": "", "updatedAt": 198},
            ]
        )

        async def rpc(machine_id, method, params):
            if method == "thread/turns/list":
                self.assertEqual(params["threadId"], "empty")
                return {"data": [], "nextCursor": None}
            self.assertEqual(method, "thread/list")
            matches = sorted(
                (
                    row
                    for row in rows
                    if row["archived"] == params["archived"]
                    and row["cwd"] in params["cwd"]
                    and params["searchTerm"] in row["name"]
                ),
                key=lambda row: row["updatedAt"],
                reverse=True,
            )
            offset, limit = int(params.get("cursor") or 0), params["limit"]
            # Native metadata omits archived; Nexus adds the queried group's status.
            return {
                "data": [
                    {key: value for key, value in row.items() if key != "archived"}
                    for row in matches[offset : offset + limit]
                ],
                "nextCursor": str(offset + limit) if offset + limit < len(matches) else None,
            }

        with patch.object(self.service, "rpc", side_effect=rpc):
            for archived in (None, False, True):
                for cwd in (None, "/second"):
                    with self.subTest(archived=archived, cwd=cwd):
                        params = {
                            "scope": "history",
                            "archived": archived,
                            "searchTerm": "topic",
                            "cwd": cwd,
                        }
                        actual, cursor = [], None
                        for page_number in range(20):
                            limit = (3, 1, 5)[page_number % 3]
                            page = await self.service.list_threads(
                                "local", {**params, "limit": limit, "cursor": cursor}
                            )
                            self.assertLessEqual(len(page["data"]), limit)
                            actual.extend(page["data"])
                            cursor = page["nextCursor"]
                            if cursor is None:
                                break
                        else:
                            self.fail("History pagination did not terminate")
                        expected = [
                            row
                            for row in rows[:17]
                            if (archived is None or row["archived"] == archived)
                            and (cwd is None or row["cwd"] == cwd)
                        ]
                        self.assertCountEqual(
                            [row["id"] for row in actual], [row["id"] for row in expected]
                        )
                        self.assertEqual(
                            [row["updatedAt"] for row in actual],
                            sorted((row["updatedAt"] for row in expected), reverse=True),
                        )
                        self.assertEqual(
                            {row["id"]: row["archived"] for row in actual},
                            {row["id"]: row["archived"] for row in expected},
                        )

    async def test_loaded_scope_applies_archive_filter_without_reading_history(self):
        self.service.machine("local")["projects"] = ["/project"]
        self.service.runtime.connections["local"] = SimpleNamespace(
            hidden_threads=set(), close=AsyncMock()
        )
        thread = {"id": "loaded", "cwd": "/project", "preview": "saved", "archived": False}

        async def rpc(machine_id, method, params):
            if method == "thread/loaded/list":
                return {"data": [thread["id"]]}
            self.assertEqual(method, "thread/read")
            return {"thread": dict(thread)}

        with patch.object(self.service, "rpc", side_effect=rpc):
            for status in ("connected", "reconnecting"):
                self.service.runtime.status["local"] = {"status": status}
                for archived in (None, False, True):
                    with self.subTest(status=status, archived=archived):
                        result = await self.service.list_threads(
                            "local", {"scope": "loaded", "archived": archived}
                        )
                        self.assertEqual(
                            [row["id"] for row in result["data"]], [] if archived else ["loaded"]
                        )
                        self.assertIsNone(result["nextCursor"])

    async def test_health_endpoint_reports_stopped_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
            ) as client:
                response = await client.get("/api/health")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["eventWorker"], "stopped")
            app.state.service.database.close()

    async def test_wait_catches_completion_during_initial_history_read(self):
        thread = {"id": "thread", "status": {"type": "active"}}
        running = {"id": "turn", "status": "inProgress", "items": []}
        done = {**running, "status": "completed"}
        calls = 0

        async def history(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                thread["status"] = {"type": "idle"}
                await self.service.events.publish(
                    "local", "turn/completed", {"threadId": "thread", "turn": done}
                )
                return {"data": [running], "nextCursor": None}
            return {"data": [done], "nextCursor": None}

        with (
            patch.object(self.service, "thread_state", new_callable=AsyncMock),
            patch.object(
                self.service,
                "read_thread",
                new_callable=AsyncMock,
                side_effect=lambda *args: {"thread": dict(thread)},
            ),
            patch.object(self.service, "history", side_effect=history),
        ):
            result = await self.service.wait_for_thread("local", "thread", 1, "turn")
        self.assertEqual(result["reason"], "turn_completed")
        self.assertEqual(result["turn"], done)
        self.assertEqual(calls, 2)

    async def test_wait_timeout_does_not_repeat_reads(self):
        with (
            patch.object(self.service, "thread_state", new_callable=AsyncMock),
            patch.object(
                self.service,
                "read_thread",
                new_callable=AsyncMock,
                return_value={"thread": {"status": {"type": "active"}}},
            ) as read,
        ):
            result = await self.service.wait_for_thread("local", "thread", 0.03)
        self.assertEqual(result["reason"], "timeout")
        read.assert_awaited_once()

    async def test_snapshot_cursor_precedes_following_deltas(self):
        service = self.service
        result = {
            "thread": {"id": "thread", "cwd": "/project"},
            "initialTurnsPage": {"data": [], "nextCursor": None},
        }

        async def rpc(method, params, *, on_response):
            service._enqueue(
                "notification",
                "local",
                source,
                ("item/agentMessage/delta", {"threadId": "thread", "delta": "before"}),
            )
            on_response(result)
            service._enqueue(
                "notification",
                "local",
                source,
                ("item/agentMessage/delta", {"threadId": "thread", "delta": "after"}),
            )
            return result

        source = SimpleNamespace(rpc=rpc, close=AsyncMock())
        service.runtime.connections["local"] = source
        snapshot = await service._rpc_connection(
            "local", source, "thread/resume", {"threadId": "thread"}, snapshot=True
        )
        await self.consumed(source)
        deltas = [
            event
            for event in service.events.history
            if event["method"] == "item/agentMessage/delta"
        ]
        self.assertEqual(snapshot["eventCursor"]["id"], deltas[0]["id"])
        self.assertGreater(deltas[1]["id"], snapshot["eventCursor"]["id"])

    async def test_old_epoch_triggers_resync_even_when_sequence_overlaps(self):
        await self.service.events.publish("local", "first", {})
        await self.service.events.publish("local", "second", {})
        iterator = self.service.events.subscribe(after=1, epoch="previous-process")
        event = await anext(iterator)
        await iterator.aclose()
        self.assertEqual(event["method"], "nexus/resync")
        self.assertEqual(event["epoch"], self.service.events.epoch)

    async def test_snapshot_reloads_range_beyond_native_page_limit(self):
        initial = {
            "thread": {},
            "data": [{"id": str(i)} for i in range(100)],
            "nextCursor": "older",
            "eventCursor": {"epoch": "instance", "id": 42},
        }
        older = {"data": [{"id": str(i)} for i in range(100, 150)], "nextCursor": "remaining"}
        with (
            patch.object(
                self.service, "thread_state", new_callable=AsyncMock, return_value={"thread": {}}
            ),
            patch.object(
                self.service, "_rpc_connection", new_callable=AsyncMock, return_value=initial
            ),
            patch.object(self.service.runtime, "connection", return_value=object()),
            patch.object(
                self.service, "history", new_callable=AsyncMock, return_value=older
            ) as history,
        ):
            result = await self.service.snapshot("local", "thread", 150)
        history.assert_awaited_once_with("local", "thread", "older", 50)
        self.assertEqual(len(result["data"]), 150)
        self.assertEqual(result["eventCursor"]["id"], 42)
        self.assertEqual(result["nextCursor"], "remaining")
