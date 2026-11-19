"""Watch Codex history through the same local/SSH app-server connection."""

import asyncio
import logging
import re
from collections import defaultdict
from pathlib import PurePosixPath
from uuid import uuid4

from .errors import NexusError

logger = logging.getLogger(__name__)
ROLLOUT_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")


class HistoryWatch:
    batch_seconds = 0.25

    def __init__(self, service):
        self.service = service
        self.tasks: dict[str, asyncio.Task] = {}
        self.watches: dict[str, dict[str, str]] = defaultdict(dict)
        self.pending: dict[str, set[str]] = defaultdict(set)
        self.signals = defaultdict(asyncio.Event)
        self.errors: dict[str, str] = {}

    def start(self, machine_id: str) -> None:
        if self.service.demo or self.service.closing or machine_id in self.tasks:
            return
        self.tasks[machine_id] = asyncio.create_task(
            self._run(machine_id), name=f"nexus-history-watch-{machine_id}"
        )

    def changed(self, machine_id: str, params: dict) -> bool:
        if params["watchId"] not in self.watches[machine_id]:
            return False
        self.pending[machine_id].update(params["changedPaths"])
        self.signals[machine_id].set()
        return True

    async def stop(self, machine_id: str) -> None:
        task = self.tasks.get(machine_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.watches.pop(machine_id, None)
        self.pending.pop(machine_id, None)
        self.signals.pop(machine_id, None)
        self.errors.pop(machine_id, None)

    async def close(self) -> None:
        await asyncio.gather(*(self.stop(machine_id) for machine_id in list(self.tasks)))

    async def _run(self, machine_id: str) -> None:
        connection = self.service.runtime.connection(machine_id)
        signal = self.signals[machine_id]
        registered: set[str] = set()

        async def rpc(method: str, params: dict):
            return await self.service._rpc_connection(machine_id, connection, method, params)

        async def watch(path: PurePosixPath) -> None:
            value = str(path)
            if value in registered:
                return
            watch_id = f"nexus-history-{uuid4()}"
            # Register ownership before the response so an immediate change is retained.
            self.watches[machine_id][watch_id] = value
            try:
                await rpc("fs/watch", {"watchId": watch_id, "path": value})
            except Exception:
                self.watches[machine_id].pop(watch_id, None)
                raise
            registered.add(value)

        async def directory(path: PurePosixPath) -> list[dict]:
            try:
                return (await rpc("fs/readDirectory", {"path": str(path)}))["entries"]
            except NexusError as exc:
                if any(
                    text in str(exc).lower()
                    for text in ("no such file", "not found", "not a directory")
                ):
                    return []
                raise

        async def tree(path: PurePosixPath) -> None:
            queue = [path]
            while queue:
                current = queue.pop()
                await watch(current)
                entries = await directory(current)
                queue.extend(
                    current / entry["fileName"]
                    for entry in entries
                    if entry["isDirectory"] and str(current / entry["fileName"]) not in registered
                )

        try:
            config = await rpc("config/read", {"includeLayers": True})
            user_layer = next(
                layer for layer in config["layers"] if layer["name"]["type"] == "user"
            )
            home = PurePosixPath(user_layer["name"]["file"]).parent
            roots = (home / "sessions", home / "archived_sessions")
            await watch(home)

            async def discover() -> None:
                entries = await directory(home)
                names = {entry["fileName"] for entry in entries if entry["isDirectory"]}
                for root in roots:
                    if root.name in names:
                        await tree(root)

            await discover()
            # Read the inventory after installing the watches to cover discovery-time writes.
            self.service.search_sync.wake(machine_id)
            while self.service.runtime.connections.get(machine_id) is connection:
                await signal.wait()
                await asyncio.sleep(self.batch_seconds)
                signal.clear()
                paths = self.pending.pop(machine_id, set())
                dirty, metadata, full, directories = set(), False, False, set()
                for value in paths:
                    path = PurePosixPath(value)
                    if any(path == root or root in path.parents for root in roots):
                        if match := ROLLOUT_ID.search(path.name):
                            dirty.add(match[1])
                        else:
                            directories.add(path)
                    elif path == home or path.parent == home:
                        if path.name == "session_index.jsonl":
                            full = True
                        elif path.name.startswith(
                            ("state_", "thread_history_")
                        ) and not path.name.endswith("-shm"):
                            metadata = True
                if directories or str(home) in paths or any(str(root) in paths for root in roots):
                    await discover()
                    full = True
                for path in directories:
                    if str(path) not in registered:
                        await tree(path)
                for thread_id in dirty:
                    key = (machine_id, thread_id)
                    known = self.service.sessions.thread_details.get(key)
                    if known and (
                        known.get("cwd") not in self.service.machine(machine_id).get("projects", [])
                        or isinstance(known.get("source"), dict)
                        and "subAgent" in known["source"]
                    ):
                        continue
                    observed = key in self.service.sessions.observed_threads
                    self.service.search_sync.wake(machine_id, thread_id, external=not observed)
                if full:
                    self.service.search_sync.wake(machine_id)
                elif metadata and not dirty:
                    self.service.search_sync.wake(machine_id, scan=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.errors[machine_id] = str(exc)
            logger.warning("Could not watch Codex history on %s: %s", machine_id, exc)
        finally:
            self.tasks.pop(machine_id, None)
