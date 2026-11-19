import asyncio
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from nexus.database import Database
from nexus.errors import NexusError
from nexus.events import EventBroker
from nexus.search import SearchIndex, SearchSync


class History:
    def __init__(self):
        self.threads = {
            str(i): {
                "id": str(i),
                "cwd": "/project",
                "name": f"thread {i}",
                "preview": "saved",
                "updatedAt": 1000 - i,
                "archived": False,
                "machineId": "machine",
            }
            for i in range(250)
        }
        self.list_calls = []
        self.item_calls = []
        self.closing = False
        self.demo = False
        self.runtime = SimpleNamespace(status={"machine": {"status": "connected"}})
        self.sessions = SimpleNamespace(fresh_threads={}, thread_details={})
        self.events = EventBroker()

    def machine(self, machine_id):
        return {"id": machine_id, "name": "machine", "projects": ["/project"]}

    async def list_threads(self, machine_id, params):
        self.list_calls.append(params)
        threads = sorted(
            (t for t in self.threads.values() if t["archived"] == params["archived"]),
            key=lambda t: t["updatedAt"],
            reverse=True,
        )
        start = int(params.get("cursor") or 0)
        return {
            "data": deepcopy(threads[start : start + 100]),
            "nextCursor": str(start + 100) if start + 100 < len(threads) else None,
        }

    async def read_thread(self, machine_id, thread_id):
        if thread_id not in self.threads:
            raise NexusError("not found", status_code=404)
        return {"thread": deepcopy(self.threads[thread_id])}

    async def rpc(self, machine_id, method, params):
        if method == "thread/turns/list":
            return {
                "data": [{"id": "turn", "status": "completed", "durationMs": 1000}],
                "nextCursor": None,
            }
        self.item_calls.append(params["threadId"])
        return {
            "data": [
                {
                    "turnId": "turn",
                    "item": {"id": "message", "type": "agentMessage", "text": "saved body"},
                }
            ],
            "nextCursor": None,
        }


class SearchSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.directory.name))
        self.index = SearchIndex(self.database)
        self.history = History()
        self.sync = SearchSync(self.history, self.index)

    async def asyncTearDown(self):
        await self.sync.close()
        self.database.close()
        self.directory.cleanup()

    async def test_event_sync_reads_only_changed_thread_and_scan_stops_at_watermark(self):
        await self.sync._sync("machine", set(), set(), full=True, scan=False)
        self.assertEqual(len(self.history.item_calls), 250)
        self.history.list_calls.clear()
        self.history.item_calls.clear()
        self.history.threads["220"]["name"] = "renamed session"
        await self.sync._sync("machine", {"220"}, set(), full=False, scan=False)
        self.assertEqual(self.history.list_calls, [])
        self.assertEqual(
            self.history.item_calls, []
        )  # Existing completed turns retain indexed content.
        result = self.index.query("renamed", "title", [("machine", "/project")])
        self.assertEqual(result["data"][0]["id"], "220")
        await self.sync._sync("machine", set(), set(), full=False, scan=True)
        self.assertEqual(len(self.history.list_calls), 2)
        self.assertTrue(all(params["useStateDbOnly"] for params in self.history.list_calls))
        self.assertEqual(self.history.item_calls, [])

    async def test_rebuild_replaces_edited_content_and_reconciliation_removes_deleted_threads(self):
        await self.sync._sync("machine", set(), set(), full=True, scan=False)
        self.history.item_calls.clear()
        await self.sync._sync("machine", {"1"}, {"1"}, full=False, scan=False)
        self.assertEqual(self.history.item_calls, ["1"])
        del self.history.threads["1"]
        await self.sync._sync("machine", set(), set(), full=True, scan=False)
        with self.index.lock:
            count = self.index.db.execute(
                "SELECT count(*) FROM threads WHERE thread_id='1'"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    async def test_deleted_attached_thread_does_not_block_other_dirty_threads(self):
        self.history.threads = {key: self.history.threads[key] for key in ("0", "1")}
        await self.sync._sync("machine", set(), set(), full=True, scan=False)
        self.history.item_calls.clear()
        read_thread = self.history.read_thread

        async def read(machine_id, thread_id):
            if thread_id == "0":
                raise NexusError("thread not loaded: 0", code=-32600)
            return await read_thread(machine_id, thread_id)

        self.history.read_thread = read
        self.history.threads["1"]["name"] = "renamed session"
        await self.sync._sync(
            "machine", {"0", "1"}, {"1"}, full=False, scan=False, external={"0", "1"}
        )

        self.assertEqual(
            self.index.query("thread 0", "title", [("machine", "/project")])["data"], []
        )
        self.assertEqual(
            self.index.query("renamed", "title", [("machine", "/project")])["data"][0]["id"], "1"
        )
        self.assertEqual(self.history.item_calls, ["1"])
        changed = [
            event["params"]["threadId"]
            for event in self.history.events.history
            if event["method"] == "nexus/history/changed"
        ]
        self.assertEqual(changed, ["1"])

    async def test_event_burst_is_coalesced(self):
        self.sync.batch_seconds = 0.01
        self.sync.reconciled["machine"] = float("inf")
        for _ in range(50):
            self.sync.wake("machine", "1")
        async with asyncio.timeout(2):
            while not self.history.item_calls:
                await asyncio.sleep(0.01)
        await self.sync.stop("machine")
        self.assertEqual(self.history.list_calls, [])
        self.assertEqual(self.history.item_calls, ["1"])
