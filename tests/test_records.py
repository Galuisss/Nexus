import json
import tempfile
import unittest
from pathlib import Path

from nexus.database import Database
from nexus.records import MessageRecords
from nexus.store import Store


class RecordTests(unittest.TestCase):
    def test_invalid_transport_timing_stays_excluded_after_replay_and_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            database = Database(directory)
            records = MessageRecords(database, Store(directory))
            records.record_origin("local", "t", "a", "agent", "tester")
            records.record_throughput("local", "t", "a", 51, 1)
            records.invalidate_throughput(["local:t:a"])
            self.assertIsNone(records.record_throughput("local", "t", "a", 51, 1))
            row = database.connection.execute(
                "SELECT tokens,duration_ms FROM message_records WHERE key='local:t:a'"
            ).fetchone()
            self.assertEqual(tuple(row), (51, 1))
            database.close()

            database = Database(directory)
            records = MessageRecords(database, Store(directory))
            turns = [{"items": [{"id": "a", "type": "agentMessage", "nexusTps": {"tokens": 51}}]}]
            records.annotate("local", "t", turns)
            item = turns[0]["items"][0]
            self.assertIsNone(item["nexusTps"])
            self.assertEqual(item["origin"]["actorName"], "tester")
            self.assertEqual(
                records.record_throughput("local", "t", "b", 51, 1000),
                {"tokens": 51, "durationMs": 1000},
            )
            database.close()

    def test_existing_records_migrate_and_new_records_do_not_rewrite_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            store = Store(directory)
            store.data.update(
                {
                    "origins": {"local:t:u": {"actor": "human", "actorName": None}},
                    "throughput": {"local:t:a": {"tokens": 32, "durationMs": 1000}},
                }
            )
            store.save()
            database = Database(directory)
            records = MessageRecords(database, store)
            original = store.path.read_bytes()
            delivery = {
                "senderBotId": "a1b2c3",
                "groupName": None,
                "cc": ["c3d4e5"],
                "promptPrefix": "From [a1b2c3], CC [c3d4e5]: ",
            }
            records.record_origin("local", "t", "u2", "agent", "tester", delivery)
            records.record_throughput("local", "t", "a2", 64, 2000)
            self.assertEqual(store.path.read_bytes(), original)
            self.assertNotIn("origins", json.loads(original))
            database.close()
            database = Database(directory)
            records = MessageRecords(database, Store(directory))
            turns = [
                {
                    "items": [
                        {"id": "native-u", "clientId": "u"},
                        {"id": "a"},
                        {"id": "u2"},
                        {"id": "a2"},
                    ]
                }
            ]
            records.annotate("local", "t", turns)
            items = turns[0]["items"]
            self.assertEqual(items[0]["origin"]["actor"], "human")
            self.assertEqual(items[1]["nexusTps"]["tokens"], 32)
            self.assertEqual(items[2]["origin"]["actorName"], "tester")
            self.assertEqual(items[2]["origin"]["delivery"], delivery)
            self.assertEqual(items[3]["nexusTps"]["tokens"], 64)
            database.close()
