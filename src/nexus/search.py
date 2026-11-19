"""Persistent local search, populated from native Codex history APIs."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections import defaultdict
from time import monotonic

from .database import Database
from .errors import NexusError
from .session_state import thread_time

logger = logging.getLogger(__name__)


def title_of(thread: dict) -> str:
    return (
        thread.get("name")
        or thread.get("title")
        or (thread.get("preview") or "新会话").split("\n")[0][:60]
    )


def message_text(item: dict) -> str:
    if item["type"] == "agentMessage":
        return item.get("text") or ""
    if item["type"] == "userMessage":
        return "\n".join(
            part["text"] for part in item.get("content", []) if part.get("type") == "text"
        )
    return ""


def turn_version(turn: dict) -> str:
    return json.dumps([turn.get(field) for field in ("status", "completedAt", "durationMs")])


class SearchIndex:
    def __init__(self, database: Database):
        self.lock = database.lock
        self.db = database.connection
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS threads (
                id INTEGER PRIMARY KEY, machine_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                cwd TEXT NOT NULL, recency_at REAL NOT NULL, archived INTEGER NOT NULL,
                metadata TEXT NOT NULL, version TEXT NOT NULL, indexed_version TEXT,
                UNIQUE(machine_id, thread_id)
            );
            CREATE TABLE IF NOT EXISTS turns (
                thread_rowid INTEGER REFERENCES threads(id) ON DELETE CASCADE,
                turn_id TEXT NOT NULL, version TEXT NOT NULL, PRIMARY KEY(thread_rowid, turn_id)
            );
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY, thread_rowid INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
                turn_id TEXT NOT NULL, item_id TEXT NOT NULL, kind TEXT NOT NULL,
                text TEXT NOT NULL, folded TEXT NOT NULL, UNIQUE(thread_rowid, turn_id, item_id)
            );
            CREATE INDEX IF NOT EXISTS documents_kind ON documents(kind);
            CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                folded, content='documents', content_rowid='id', tokenize='trigram'
            );
            CREATE TRIGGER IF NOT EXISTS documents_insert AFTER INSERT ON documents BEGIN
                INSERT INTO documents_fts(rowid, folded) VALUES (new.id, new.folded);
            END;
            CREATE TRIGGER IF NOT EXISTS documents_delete AFTER DELETE ON documents BEGIN
                INSERT INTO documents_fts(documents_fts, rowid, folded) VALUES ('delete', old.id, old.folded);
            END;
            CREATE TRIGGER IF NOT EXISTS documents_update AFTER UPDATE ON documents BEGIN
                INSERT INTO documents_fts(documents_fts, rowid, folded) VALUES ('delete', old.id, old.folded);
                INSERT INTO documents_fts(rowid, folded) VALUES (new.id, new.folded);
            END;
        """)
        with self.lock, self.db:
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(threads)")}
            if "updated_at" in columns:
                self.db.execute("ALTER TABLE threads RENAME COLUMN updated_at TO recency_at")
                self.db.execute(
                    "UPDATE threads SET recency_at=COALESCE(json_extract(metadata, '$.recencyAt'), recency_at)"
                )
                self.db.execute("""UPDATE threads SET
                    indexed_version=CASE WHEN indexed_version=version THEN CAST(CAST(recency_at AS INTEGER) AS TEXT) END,
                    version=CAST(CAST(recency_at AS INTEGER) AS TEXT)""")
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS threads_scope ON threads(machine_id, cwd, archived, recency_at DESC)"
            )

    def remember(self, machine_id: str, threads: list[dict]) -> tuple[list[dict], list[dict]]:
        pending, changed = [], []
        with self.lock, self.db:
            for thread in threads:
                metadata = {key: value for key, value in thread.items() if key != "turns"}
                previous = self.db.execute(
                    "SELECT metadata FROM threads WHERE machine_id=? AND thread_id=?",
                    (machine_id, thread["id"]),
                ).fetchone()
                before = json.loads(previous[0]) if previous else None
                fields = (
                    "name",
                    "title",
                    "preview",
                    "cwd",
                    "archived",
                    "updatedAt",
                    "recencyAt",
                    "status",
                    "path",
                )
                if before is None or any(
                    before.get(field) != metadata.get(field) for field in fields
                ):
                    changed.append(metadata)
                version = str(thread_time(thread))
                row = self.db.execute(
                    """
                    INSERT INTO threads(machine_id, thread_id, cwd, recency_at, archived, metadata, version)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(machine_id, thread_id) DO UPDATE SET cwd=excluded.cwd,
                        recency_at=excluded.recency_at, archived=excluded.archived,
                        metadata=excluded.metadata, version=excluded.version
                    RETURNING id, indexed_version
                """,
                    (
                        machine_id,
                        thread["id"],
                        thread["cwd"],
                        thread_time(thread),
                        bool(thread.get("archived")),
                        json.dumps(metadata, ensure_ascii=False),
                        version,
                    ),
                ).fetchone()
                title = title_of(thread)
                self.db.execute(
                    """
                    INSERT INTO documents(thread_rowid, turn_id, item_id, kind, text, folded)
                    VALUES (?, '', '', 'title', ?, ?)
                    ON CONFLICT(thread_rowid, turn_id, item_id) DO UPDATE SET text=excluded.text, folded=excluded.folded
                    WHERE documents.text != excluded.text
                """,
                    (row["id"], title, title.casefold()),
                )
                if (
                    row["indexed_version"] != version
                    or thread.get("status", {}).get("type") == "active"
                    or before
                    and before.get("status", {}).get("type") == "active"
                ):
                    pending.append(thread)
        return pending, changed

    def all_addresses(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT machine_id, metadata FROM threads").fetchall()
        return [{**json.loads(row["metadata"]), "machineId": row["machine_id"]} for row in rows]

    def find_addresses(self, identifier: str, machine_id: str | None = None) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT machine_id, metadata FROM threads WHERE (thread_id=? OR substr(thread_id, -6)=?)"
                + (" AND machine_id=?" if machine_id else ""),
                (identifier, identifier, machine_id) if machine_id else (identifier, identifier),
            ).fetchall()
        return [{**json.loads(row["metadata"]), "machineId": row["machine_id"]} for row in rows]

    def versions(self, machine_id: str, thread_id: str) -> tuple[int, bool, dict[str, str]]:
        with self.lock:
            row = self.db.execute(
                "SELECT id, indexed_version FROM threads WHERE machine_id=? AND thread_id=?",
                (machine_id, thread_id),
            ).fetchone()
            versions = dict(
                self.db.execute(
                    "SELECT turn_id, version FROM turns WHERE thread_rowid=?", (row["id"],)
                )
            )
            return row["id"], row["indexed_version"] is not None, versions

    def replace_turns(
        self, rowid: int, turns: list[dict], entries: list[dict], changed: set[str], version: str
    ) -> None:
        with self.lock, self.db:
            retained = {turn["id"] for turn in turns}
            old = {
                row[0]
                for row in self.db.execute(
                    "SELECT turn_id FROM turns WHERE thread_rowid=?", (rowid,)
                )
            }
            for turn_id in changed | (old - retained):
                self.db.execute(
                    "DELETE FROM documents WHERE thread_rowid=? AND turn_id=? AND kind='content'",
                    (rowid, turn_id),
                )
                self.db.execute(
                    "DELETE FROM turns WHERE thread_rowid=? AND turn_id=?", (rowid, turn_id)
                )
            for entry in entries:
                item = entry["item"]
                text = message_text(item)
                if text and entry["turnId"] in retained:
                    self.db.execute(
                        """INSERT INTO documents(thread_rowid, turn_id, item_id, kind, text, folded)
                                       VALUES (?, ?, ?, 'content', ?, ?)""",
                        (rowid, entry["turnId"], item["id"], text, text.casefold()),
                    )
            self.db.executemany(
                "INSERT OR REPLACE INTO turns VALUES (?, ?, ?)",
                [(rowid, turn["id"], turn_version(turn)) for turn in turns],
            )
            self.db.execute("UPDATE threads SET indexed_version=? WHERE id=?", (version, rowid))

    def prune(self, machine_id: str, retained: set[str]) -> list[str]:
        with self.lock, self.db:
            rows = self.db.execute(
                "SELECT id, thread_id FROM threads WHERE machine_id=?", (machine_id,)
            ).fetchall()
            removed = [row for row in rows if row["thread_id"] not in retained]
            self.db.executemany("DELETE FROM threads WHERE id=?", [(row["id"],) for row in removed])
            return [row["thread_id"] for row in removed]

    def remove(self, machine_id: str, thread_id: str | None = None) -> None:
        with self.lock, self.db:
            self.db.execute(
                "DELETE FROM threads WHERE machine_id=?"
                + (" AND thread_id=?" if thread_id else ""),
                (machine_id, thread_id) if thread_id else (machine_id,),
            )

    def query(
        self,
        term: str,
        mode: str,
        projects: list[tuple[str, str]],
        *,
        allowed: set[tuple[str, str]] | None = None,
        archived: bool | None = None,
        cursor: str | None = None,
        limit: int = 10,
    ) -> dict:
        if not term or not projects or allowed == set():
            return {"data": [], "nextCursor": None}
        folded = term.casefold()
        args: list = []
        if mode == "id":
            statement = "SELECT t.*, NULL AS text, NULL AS kind FROM threads t"
        else:
            match = "instr(d.folded, ?) > 0"
            args.append(folded)
            join = ""
            if len(folded) >= 3:
                join = "JOIN documents_fts ON documents_fts.rowid=d.id"
                match += " AND documents_fts MATCH ?"
                args.append('"' + folded.replace('"', '""') + '"')
            if mode != "both":
                match += " AND d.kind=?"
                args.append(mode)
            statement = f"""WITH matches AS (
                SELECT d.thread_rowid, min(d.id) AS document_id FROM documents d {join}
                WHERE {match} GROUP BY d.thread_rowid
            )
            SELECT t.*, d.text, d.kind FROM matches m JOIN threads t ON t.id=m.thread_rowid
                JOIN documents d ON d.id=m.document_id"""
        scope = "(" + " OR ".join("(t.machine_id=? AND t.cwd=?)" for _ in projects) + ")"
        args.extend(part for project in projects for part in project)
        if mode == "id":
            scope += " AND substr(lower(t.thread_id), -length(?))=?"
            args.extend([folded, folded])
        if allowed is not None:
            scope += (
                " AND (t.machine_id || '/' || t.thread_id) IN ("
                + ",".join("?" for _ in allowed)
                + ")"
            )
            args.extend(machine + "/" + thread for machine, thread in allowed)
        if archived is not None:
            scope += " AND t.archived=?"
            args.append(archived)
        if cursor:
            try:
                recency, rowid = json.loads(base64.urlsafe_b64decode(cursor))
            except (ValueError, TypeError) as exc:
                raise NexusError("搜索分页位置无效") from exc
            scope += " AND (t.recency_at < ? OR (t.recency_at=? AND t.id < ?))"
            args.extend([recency, recency, rowid])
        args.append(limit + 1)
        with self.lock:
            rows = self.db.execute(
                f"""
                {statement}
                WHERE {scope} ORDER BY t.recency_at DESC, t.id DESC LIMIT ?
            """,
                args,
            ).fetchall()
        data = []
        for row in rows[:limit]:
            thread = json.loads(row["metadata"])
            if row["kind"] == "content":
                text = row["text"]
                start = max(0, text.casefold().find(folded) - 35)
                thread["searchMatch"] = {
                    "text": ("…" if start else "")
                    + text[start : start + 160]
                    + ("…" if len(text) > start + 160 else "")
                }
            data.append(thread)
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps([last["recency_at"], last["id"]]).encode()
            ).decode()
        return {"data": data, "nextCursor": next_cursor}


class SearchSync:
    reconcile_seconds = 300
    batch_seconds = 0.25

    def __init__(self, service, index: SearchIndex):
        self.service = service
        self.index = index
        self.tasks: dict[str, asyncio.Task] = {}
        self.signals = defaultdict(asyncio.Event)
        self.dirty = defaultdict(set)
        self.content = defaultdict(set)
        self.rebuild = defaultdict(set)
        self.full: set[str] = set()
        self.scans: set[str] = set()
        self.external = defaultdict(set)
        self.reconciled: dict[str, float] = {}
        self.watermarks: dict[tuple[str, bool], int] = {}
        self.busy: set[str] = set()
        self.errors: dict[str, str] = {}

    def wake(
        self,
        machine_id: str,
        thread_id: str | None = None,
        *,
        rebuild: bool = False,
        content: bool = False,
        scan: bool = False,
        external: bool = False,
    ) -> None:
        if self.service.closing or (
            not self.service.demo
            and self.service.runtime.status.get(machine_id, {}).get("status") != "connected"
        ):
            return
        if thread_id:
            self.dirty[machine_id].add(thread_id)
            if content:
                self.content[machine_id].add(thread_id)
            if rebuild:
                self.rebuild[machine_id].add(thread_id)
            if external:
                self.external[machine_id].add(thread_id)
        elif scan:
            self.scans.add(machine_id)
        else:
            self.full.add(machine_id)
        self.signals[machine_id].set()
        if machine_id not in self.tasks:
            self.tasks[machine_id] = asyncio.create_task(
                self._run(machine_id), name=f"nexus-search-{machine_id}"
            )

    def forget(self, machine_id: str, thread_id: str) -> None:
        self.dirty[machine_id].discard(thread_id)
        self.content[machine_id].discard(thread_id)
        self.rebuild[machine_id].discard(thread_id)
        self.external[machine_id].discard(thread_id)

    async def stop(self, machine_id: str) -> None:
        task = self.tasks.get(machine_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def close(self) -> None:
        await asyncio.gather(*(self.stop(machine_id) for machine_id in list(self.tasks)))

    async def _publish(self, machine_id: str) -> None:
        await self.service.events.publish(machine_id, "nexus/search/updated", {})

    async def _publish_threads(self, machine_id: str, updated: list[dict], removed=()) -> None:
        if not updated and not removed:
            return
        details = self.service.sessions.thread_details
        await self.service.events.publish(
            machine_id,
            "nexus/threads/changed",
            {
                "updated": [
                    {**thread, **details.get((machine_id, thread["id"]), {})} for thread in updated
                ],
                "removedThreadIds": sorted(removed),
            },
        )

    async def _run(self, machine_id: str) -> None:
        signal = self.signals[machine_id]
        next_reconcile = (
            self.reconciled.get(machine_id, monotonic() - self.reconcile_seconds)
            + self.reconcile_seconds
        )
        retry_delay = 1
        try:
            while True:
                await asyncio.sleep(self.batch_seconds)
                signal.clear()
                scan = machine_id in self.scans
                full = machine_id in self.full or monotonic() >= next_reconcile
                self.full.discard(machine_id)
                self.scans.discard(machine_id)
                dirty = self.dirty.pop(machine_id, set())
                content = self.content.pop(machine_id, set())
                rebuild = self.rebuild.pop(machine_id, set())
                external = self.external.pop(machine_id, set())
                self.busy.add(machine_id)
                delay = self.reconcile_seconds
                try:
                    await self._sync(
                        machine_id,
                        dirty,
                        rebuild,
                        full=full,
                        scan=scan,
                        external=external,
                        content=content,
                    )
                    self.errors.pop(machine_id, None)
                    retry_delay = 1
                except Exception as exc:
                    self.dirty[machine_id].update(dirty)
                    self.content[machine_id].update(content)
                    self.rebuild[machine_id].update(rebuild)
                    if full:
                        self.full.add(machine_id)
                    if scan:
                        self.scans.add(machine_id)
                    self.external[machine_id].update(external)
                    self.errors[machine_id] = str(exc)
                    logger.warning("Could not sync search on %s: %s", machine_id, exc)
                    delay = retry_delay
                    retry_delay = min(retry_delay * 2, 30)
                finally:
                    self.busy.discard(machine_id)
                    await self._publish(machine_id)
                if full:
                    next_reconcile = monotonic() + self.reconcile_seconds
                try:
                    await asyncio.wait_for(
                        signal.wait(), timeout=min(delay, max(0, next_reconcile - monotonic()))
                    )
                except TimeoutError:
                    pass
        finally:
            self.busy.discard(machine_id)
            self.tasks.pop(machine_id, None)

    async def _scan(self, machine_id: str, full: bool) -> tuple[dict[str, dict], set[str]]:
        threads, needed = {}, set()
        for archived in (False, True):
            cursor = None
            watermark = self.watermarks.get((machine_id, archived), 0)
            newest = watermark
            while True:
                result = await self.service.list_threads(
                    machine_id,
                    {
                        "scope": "history",
                        "archived": archived,
                        "limit": 100,
                        "cursor": cursor,
                        "useStateDbOnly": not full,
                    },
                )
                page = result["data"]
                threads.update((thread["id"], thread) for thread in page)
                pending, updates = await asyncio.to_thread(self.index.remember, machine_id, page)
                needed.update(thread["id"] for thread in pending)
                await self._publish_threads(machine_id, updates)
                if full:
                    await self._publish(machine_id)
                newest = max([newest, *(thread_time(thread) for thread in page)])
                cursor = result.get("nextCursor")
                if not cursor or (
                    not full and any(thread_time(thread) < watermark for thread in page)
                ):
                    break
            self.watermarks[(machine_id, archived)] = newest
        return threads, needed

    async def _sync(
        self,
        machine_id: str,
        dirty: set[str],
        rebuild: set[str],
        *,
        full: bool,
        scan: bool,
        external: set[str] | None = None,
        content: set[str] | None = None,
    ) -> None:
        machine = self.service.machine(machine_id)
        threads, needed = await self._scan(machine_id, full) if full or scan else ({}, set())
        removed = set()
        for (host, thread_id), thread in list(self.service.sessions.fresh_threads.items()):
            if host == machine_id and thread.get("cwd") in machine.get("projects", []):
                threads.setdefault(
                    thread_id, {**thread, "machineId": host, "machineName": machine["name"]}
                )
                dirty.add(thread_id)
        semaphore = asyncio.Semaphore(4)

        async def read_dirty(thread_id: str):
            async with semaphore:
                try:
                    thread = (await self.service.read_thread(machine_id, thread_id))["thread"]
                except NexusError as exc:
                    if (
                        exc.status_code == 404
                        or exc.code == -32600
                        and (
                            "not found" in exc.message
                            or exc.message == f"thread not loaded: {thread_id}"
                        )
                    ):
                        threads.pop(thread_id, None)
                        needed.discard(thread_id)
                        await asyncio.to_thread(self.index.remove, machine_id, thread_id)
                        removed.add(thread_id)
                        return
                    raise
                if (
                    thread.get("cwd") not in machine.get("projects", [])
                    or thread.get("ephemeral")
                    or thread.get("threadSource") == "nexus-side"
                    or isinstance(thread.get("source"), dict)
                    and "subAgent" in thread["source"]
                ):
                    threads.pop(thread_id, None)
                    needed.discard(thread_id)
                    await asyncio.to_thread(self.index.remove, machine_id, thread_id)
                    return
                threads[thread_id] = {
                    **thread,
                    "machineId": machine_id,
                    "machineName": machine["name"],
                }

        reads = await asyncio.gather(
            *(read_dirty(thread_id) for thread_id in dirty), return_exceptions=True
        )
        if any(isinstance(result, Exception) for result in reads):
            raise next(result for result in reads if isinstance(result, Exception))
        pending, updates = await asyncio.to_thread(
            self.index.remember,
            machine_id,
            [threads[thread_id] for thread_id in dirty & threads.keys()],
        )
        needed.update(thread["id"] for thread in pending)
        needed.update((rebuild | (content or set())) & threads.keys())
        if full:
            removed.update(await asyncio.to_thread(self.index.prune, machine_id, set(threads)))
            self.reconciled[machine_id] = monotonic()
        await self._publish_threads(machine_id, updates, removed)
        await self._publish(machine_id)

        async def sync(thread: dict):
            async with semaphore:
                await self._sync_thread(machine_id, thread, thread["id"] in rebuild)

        sync_ids = sorted(needed)
        results = await asyncio.gather(
            *(sync(threads[thread_id]) for thread_id in sync_ids), return_exceptions=True
        )
        failures = {
            thread_id
            for thread_id, result in zip(sync_ids, results)
            if isinstance(result, Exception)
        }
        if failures:
            self.dirty[machine_id].update(failures)
            self.rebuild[machine_id].update(rebuild & failures)
            raise next(result for result in results if isinstance(result, Exception))
        for thread_id in (external or set()) - removed:
            await self.service.events.publish(
                machine_id, "nexus/history/changed", {"threadId": thread_id}
            )

    async def _pages(self, machine_id: str, method: str, params: dict) -> list[dict]:
        data, cursor = [], None
        while True:
            page = await self.service.rpc(
                machine_id, method, {**params, "cursor": cursor, "limit": 100}
            )
            data.extend(
                entry
                for entry in page["data"]
                if method != "thread/items/list"
                or entry["item"]["type"] in {"userMessage", "agentMessage"}
            )
            cursor = page.get("nextCursor")
            if not cursor:
                return data

    async def _sync_thread(self, machine_id: str, thread: dict, rebuild: bool) -> None:
        thread_id = thread["id"]
        rowid, indexed, versions = await asyncio.to_thread(
            self.index.versions, machine_id, thread_id
        )
        fresh = self.service.sessions.fresh_threads.get((machine_id, thread_id))
        if fresh and not fresh.get("turns"):
            turns, entries = [], []
            changed = set(versions)
        else:
            turns = await self._pages(
                machine_id,
                "thread/turns/list",
                {
                    "threadId": thread_id,
                    "itemsView": "notLoaded",
                    "sortDirection": "asc",
                },
            )
            changed = {
                turn["id"]
                for turn in turns
                if rebuild
                or versions.get(turn["id"]) != turn_version(turn)
                or turn["status"] in {"inProgress", "active"}
            }
            params = {
                "threadId": thread_id,
                "sortDirection": "asc",
                "deferOutput": {"commandExecution": True, "mcpToolCall": True},
            }
            entries = []
            if not indexed or rebuild:
                changed.update(versions)
                entries = await self._pages(machine_id, "thread/items/list", params)
            else:
                for turn_id in changed:
                    entries.extend(
                        await self._pages(
                            machine_id, "thread/items/list", {**params, "turnId": turn_id}
                        )
                    )
        await asyncio.to_thread(
            self.index.replace_turns, rowid, turns, entries, changed, str(thread_time(thread))
        )
