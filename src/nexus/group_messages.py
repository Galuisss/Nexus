"""Persistent public group messages and their delivery results."""

import json
from uuid import uuid4

from .errors import NexusError
from .events import now


class GroupMessages:
    def __init__(self, database):
        self.db = database.connection
        self.lock = database.lock
        with self.lock, self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS group_messages (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE, group_name TEXT NOT NULL,
                    sender_kind TEXT NOT NULL, sender_bot_id TEXT, sender_thread_id TEXT,
                    text TEXT NOT NULL, created_at TEXT NOT NULL,
                    result TEXT
                );
                CREATE INDEX IF NOT EXISTS group_messages_history ON group_messages(group_name, sequence);
            """)

    @staticmethod
    def message(row):
        result = json.loads(row["result"]) if row["result"] else None
        if result is None:
            status = "pending"
        elif result["errors"]:
            status = "partial" if result["data"] else "failed"
        else:
            status = "sent"
        return {
            "id": row["id"],
            "sequence": row["sequence"],
            "groupName": row["group_name"],
            "senderKind": row["sender_kind"],
            "senderBotId": row["sender_bot_id"],
            "senderThreadId": row["sender_thread_id"],
            "text": row["text"],
            "createdAt": row["created_at"],
            "status": status,
            "revision": int(result is not None),
            "delivery": result,
        }

    def create(self, group, kind, sender, thread, text, client_id=None):
        message_id = client_id or uuid4().hex
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM group_messages WHERE id=?", (message_id,)
            ).fetchone()
            if row:
                if row["group_name"] != group or row["sender_kind"] != kind or row["text"] != text:
                    raise NexusError("消息 ID 已被使用", status_code=409)
                return self.message(row), False
            row = self.db.execute(
                """
                INSERT INTO group_messages(id,group_name,sender_kind,sender_bot_id,sender_thread_id,text,created_at)
                VALUES (?,?,?,?,?,?,?) RETURNING *
            """,
                (message_id, group, kind, sender, thread, text, now()),
            ).fetchone()
            return self.message(row), True

    def finish(self, message_id, result):
        with self.lock, self.db:
            row = self.db.execute(
                "UPDATE group_messages SET result=? WHERE id=? RETURNING *",
                (json.dumps(result, ensure_ascii=False), message_id),
            ).fetchone()
            return self.message(row)

    def history(self, group, before=None, limit=50):
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM group_messages WHERE group_name=?"
                + (" AND sequence<?" if before is not None else "")
                + " ORDER BY sequence DESC LIMIT ?",
                (group, before, limit + 1) if before is not None else (group, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        return {
            "data": [self.message(row) for row in reversed(rows)],
            "nextCursor": rows[-1]["sequence"] if more else None,
        }

    def summaries(self):
        with self.lock:
            rows = self.db.execute("""
                SELECT m.*, latest.total FROM group_messages m JOIN (
                    SELECT group_name, max(sequence) AS sequence, count(*) AS total
                    FROM group_messages GROUP BY group_name
                ) latest ON m.sequence=latest.sequence
            """).fetchall()
        return {
            row["group_name"]: {
                "messageCount": row["total"],
                "lastMessage": {
                    "text": row["text"][:160],
                    "createdAt": row["created_at"],
                    "sequence": row["sequence"],
                    "senderKind": row["sender_kind"],
                    "senderBotId": row["sender_bot_id"],
                },
            }
            for row in rows
        }

    def delete(self, group):
        with self.lock, self.db:
            if self.db.execute(
                "SELECT 1 FROM group_messages WHERE group_name=? AND result IS NULL", (group,)
            ).fetchone():
                raise NexusError("群消息正在发送，请稍后删除", status_code=409)
            self.db.execute("DELETE FROM group_messages WHERE group_name=?", (group,))
