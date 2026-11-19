"""Persist native response attribution, including observations of tool definitions."""

import json


class ContextRecords:
    def __init__(self, database):
        self.db, self.lock = database.connection, database.lock
        with self.lock, self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS context_items (
                    machine TEXT, id TEXT, item TEXT NOT NULL, PRIMARY KEY(machine, id)
                );
                CREATE TABLE IF NOT EXISTS context_responses (
                    machine TEXT, thread TEXT, id TEXT, model TEXT, attribution TEXT NOT NULL,
                    PRIMARY KEY(machine, id)
                );
                CREATE INDEX IF NOT EXISTS context_responses_thread ON context_responses(machine, thread);
                CREATE TABLE IF NOT EXISTS context_tools (
                    model TEXT, definition TEXT, tokens INTEGER NOT NULL, PRIMARY KEY(model, definition)
                );
            """)

    def record_item(self, machine, item):
        if not item.get("id"):
            return
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO context_items VALUES (?, ?, ?)",
                (machine, item["id"], json.dumps(item, ensure_ascii=False)),
            )

    def record_response(self, machine, model, params):
        metadata = (params.get("usageMetadata") or {}).get("metadata") or {}
        attribution = metadata.get("attribution", {}).get("items")
        if attribution is None:
            return
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO context_responses VALUES (?, ?, ?, ?, ?)",
                (machine, params["threadId"], params["responseId"], model, json.dumps(attribution)),
            )
            for item_id, usage in attribution.items():
                if not usage.get("input_tokens"):
                    continue
                row = self.db.execute(
                    "SELECT item FROM context_items WHERE machine=? AND id=?", (machine, item_id)
                ).fetchone()
                if not row:
                    continue
                item = json.loads(row["item"])
                if item["type"] in {"additional_tools", "tool_search_output"}:
                    self.db.execute(
                        "INSERT OR REPLACE INTO context_tools VALUES (?, ?, ?)",
                        (model, tool_key(item), usage["input_tokens"]),
                    )

    def read(self, machine, threads):
        responses, items = {}, {}
        with self.lock:
            for thread in threads:
                for row in self.db.execute(
                    "SELECT id, attribution FROM context_responses WHERE machine=? AND thread=?",
                    (machine, thread),
                ):
                    attribution = json.loads(row["attribution"])
                    responses[row["id"]] = attribution
                    for item_id in attribution:
                        if item_id in items:
                            continue
                        item = self.db.execute(
                            "SELECT item FROM context_items WHERE machine=? AND id=?",
                            (machine, item_id),
                        ).fetchone()
                        if item:
                            items[item_id] = json.loads(item["item"])
            tools = {
                (row["model"], row["definition"]): row["tokens"]
                for row in self.db.execute("SELECT * FROM context_tools")
            }
        return {"responses": responses, "items": items, "tools": tools}


def tool_key(item):
    return json.dumps(
        item.get("tools", []), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
