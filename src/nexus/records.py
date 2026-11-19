"""Message attribution and throughput, stored independently of indexed history."""

import json
from functools import lru_cache

from .database import Database
from .store import Store


class MessageRecords:
    def __init__(self, database: Database, store: Store):
        self.db = database.connection
        self.lock = database.lock
        with self.lock, self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS message_records (
                key TEXT PRIMARY KEY, origin TEXT, tokens INTEGER, duration_ms INTEGER
            )""")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(message_records)")}
            if "timing_invalid" not in columns:
                self.db.execute(
                    "ALTER TABLE message_records ADD COLUMN timing_invalid INTEGER NOT NULL DEFAULT 0"
                )
            self.db.executemany(
                "INSERT OR IGNORE INTO message_records(key, origin) VALUES (?, ?)",
                [
                    (key, json.dumps(value, ensure_ascii=False))
                    for key, value in store.data.get("origins", {}).items()
                ],
            )
            self.db.executemany(
                """INSERT INTO message_records(key, tokens, duration_ms) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET tokens=excluded.tokens, duration_ms=excluded.duration_ms""",
                [
                    (key, value["tokens"], value["durationMs"])
                    for key, value in store.data.get("throughput", {}).items()
                ],
            )
        if "origins" in store.data or "throughput" in store.data:
            store.data.pop("origins", None)
            store.data.pop("throughput", None)
            store.save()

    def read(self, machine_id: str, thread_id: str, item_id: str) -> dict:
        with self.lock:
            return self._read(machine_id, thread_id, item_id)

    @lru_cache(maxsize=4096)
    def _read(self, machine_id: str, thread_id: str, item_id: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM message_records WHERE key=?", (f"{machine_id}:{thread_id}:{item_id}",)
        ).fetchone()
        return {
            "origin": json.loads(row["origin"]) if row and row["origin"] else None,
            "nexusTps": {"tokens": row["tokens"], "durationMs": row["duration_ms"]}
            if row and row["tokens"] is not None and not row["timing_invalid"]
            else None,
        }

    def record_origin(
        self,
        machine_id: str,
        thread_id: str,
        item_id: str,
        actor: str,
        actor_name: str | None,
        delivery: dict | None = None,
    ) -> None:
        origin = {"actor": actor, "actorName": actor_name}
        if delivery is not None:
            origin["delivery"] = delivery
        with self.lock, self.db:
            self.db.execute(
                """INSERT INTO message_records(key, origin) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET origin=excluded.origin""",
                (
                    f"{machine_id}:{thread_id}:{item_id}",
                    json.dumps(origin, ensure_ascii=False),
                ),
            )
            self._read.cache_clear()

    def record_throughput(
        self, machine_id: str, thread_id: str, item_id: str, tokens: int, duration_ms: int
    ) -> dict | None:
        with self.lock, self.db:
            self.db.execute(
                """INSERT INTO message_records(key, tokens, duration_ms) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET tokens=excluded.tokens, duration_ms=excluded.duration_ms
                WHERE message_records.timing_invalid=0""",
                (f"{machine_id}:{thread_id}:{item_id}", tokens, duration_ms),
            )
            self._read.cache_clear()
        return self.read(machine_id, thread_id, item_id)["nexusTps"]

    def invalidate_throughput(self, keys: list[str]) -> None:
        """Keep the original observations while excluding invalid transport timing."""
        with self.lock, self.db:
            self.db.executemany(
                "UPDATE message_records SET timing_invalid=1 WHERE key=?", [(key,) for key in keys]
            )
            self._read.cache_clear()

    def annotate(self, machine_id: str, thread_id: str, turns: list[dict]) -> None:
        for turn in turns:
            for item in turn.get("items", []):
                record = self.read(machine_id, thread_id, item.get("id", ""))
                origin = (
                    self.read(machine_id, thread_id, item["clientId"])["origin"]
                    if item.get("clientId")
                    else record["origin"]
                )
                if origin:
                    item["origin"] = origin
                if record["nexusTps"]:
                    item["nexusTps"] = record["nexusTps"]
                elif item.get("type") == "agentMessage":
                    item["nexusTps"] = None
