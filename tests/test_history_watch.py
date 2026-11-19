import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from nexus.history_watch import HistoryWatch


class HistoryWatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_directory_is_watched_and_external_rollout_wakes_index(self):
        directories = {
            "/codex": ["sessions"],
            "/codex/sessions": ["2026"],
            "/codex/sessions/2026": ["11"],
            "/codex/sessions/2026/11": ["16"],
            "/codex/sessions/2026/11/16": [],
        }
        registered = {}
        connection = object()
        wake = Mock()

        async def rpc(machine_id, source, method, params):
            self.assertIs(source, connection)
            if method == "config/read":
                return {"layers": [{"name": {"type": "user", "file": "/codex/config.toml"}}]}
            if method == "fs/watch":
                registered[params["path"]] = params["watchId"]
                return {"path": params["path"]}
            self.assertEqual(method, "fs/readDirectory")
            return {
                "entries": [
                    {"fileName": name, "isDirectory": True}
                    for name in directories.get(params["path"], [])
                ]
            }

        service = SimpleNamespace(
            demo=False,
            closing=False,
            _rpc_connection=rpc,
            runtime=SimpleNamespace(
                connection=lambda _: connection, connections={"machine": connection}
            ),
            sessions=SimpleNamespace(observed_threads=set(), thread_details={}),
            search_sync=SimpleNamespace(wake=wake),
        )
        watcher = HistoryWatch(service)
        watcher.batch_seconds = 0.01
        watcher.start("machine")

        async def until(predicate):
            async with asyncio.timeout(1):
                while not predicate():
                    await asyncio.sleep(0.005)

        try:
            await until(lambda: wake.called)
            self.assertIn("/codex/sessions/2026/11/16", registered)
            thread = "01234567-1234-1234-1234-123456789abc"
            watcher.changed(
                "machine",
                {
                    "watchId": registered["/codex/sessions/2026/11/16"],
                    "changedPaths": [f"/codex/sessions/2026/11/16/rollout-{thread}.jsonl"],
                },
            )
            await until(
                lambda: any(call.args == ("machine", thread) for call in wake.call_args_list)
            )
            self.assertTrue(
                next(call.kwargs["external"] for call in wake.call_args_list if len(call.args) == 2)
            )
            directories["/codex/sessions/2026/11"].append("17")
            directories["/codex/sessions/2026/11/17"] = []
            watcher.changed(
                "machine",
                {
                    "watchId": registered["/codex/sessions/2026/11"],
                    "changedPaths": ["/codex/sessions/2026/11/17"],
                },
            )
            await until(lambda: "/codex/sessions/2026/11/17" in registered)
        finally:
            await watcher.close()
        self.assertFalse(watcher.tasks)
        self.assertFalse(watcher.watches)
