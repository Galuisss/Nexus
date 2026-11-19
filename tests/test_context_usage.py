import tempfile
import unittest
from pathlib import Path

from nexus.context_records import ContextRecords, tool_key
from nexus.context_usage import analyze_records, item_costs, text_tokens
from nexus.database import Database


class ContextUsageTests(unittest.TestCase):
    def test_text_parts_share_bpe_boundaries(self):
        item = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "a"}, {"type": "input_text", "text": "b"}],
        }
        self.assertEqual(sum(item_costs(item, {}, "").values()), text_tokens("ab", ""))
        self.assertLess(text_tokens("ab", ""), text_tokens("a", "") + text_tokens("b", ""))

    def test_native_input_output_and_cache_align_with_discarded_output_excluded(self):
        tools = {"id": "tools", "type": "additional_tools", "tools": [{"name": "probe"}]}
        user = {
            "id": "user",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Hi"}],
        }
        reasoning = {"id": "reasoning", "type": "reasoning", "encrypted_content": "opaque"}
        answer = {
            "id": "answer",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "OK"}],
        }
        lost = {
            "id": "lost",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Dropped"}],
        }
        attrs = {
            "tools": {"input_tokens": 31, "cached_tokens": 20, "cache_write_tokens": 4},
            "user": {
                "input_tokens": 5,
                "cached_tokens": 3,
                "content": [{"input_tokens": 5, "cached_tokens": 3}],
            },
            "reasoning": {"output_tokens": 7},
            "answer": {
                "input_tokens": 2,
                "output_tokens": 2,
                "content": [{"input_tokens": 2, "output_tokens": 2}],
            },
            "lost": {"output_tokens": 3},
        }
        records = [{"type": "turn_context", "payload": {"model": "model"}}]
        records += [
            {"type": "response_item", "payload": item} for item in [tools, user, reasoning, answer]
        ]
        records += [
            {
                "type": "token_usage_record",
                "payload": {
                    "thread_id": "thread",
                    "response_id": "response",
                    "usage": {
                        "input_tokens": 38,
                        "output_tokens": 12,
                        "total_tokens": 50,
                        "reasoning_output_tokens": 7,
                        "cached_input_tokens": 23,
                        "cache_write_input_tokens": 4,
                    },
                },
            }
        ]
        result = analyze_records(
            records, native={"responses": {"response": attrs}, "items": {"lost": lost}}
        )
        self.assertEqual(result["attribution"], "native")
        self.assertEqual(result["input"]["delta"], 0)
        self.assertEqual(result["output"]["delta"], 0)
        self.assertEqual(result["cache"]["delta"], 0)
        self.assertEqual(result["cache"]["writeDelta"], 0)
        self.assertEqual(result["discardedOutputTokens"], 3)
        self.assertEqual(result["usedTokens"], 47)
        self.assertEqual(sum(row["tokens"] or 0 for row in result["categories"]), 47)
        self.assertEqual(
            next(row["tokens"] for row in result["categories"] if row["id"] == "reasoning"), 7
        )

    def test_native_attribution_and_tool_observations_survive_restart(self):
        tools = {"id": "tools", "type": "additional_tools", "tools": [{"name": "probe"}]}
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory))
            records = ContextRecords(db)
            records.record_item("local", tools)
            records.record_response(
                "local",
                "model",
                {
                    "threadId": "thread",
                    "responseId": "response",
                    "usageMetadata": {
                        "metadata": {"attribution": {"items": {"tools": {"input_tokens": 31}}}}
                    },
                },
            )
            db.close()
            db = Database(Path(directory))
            native = ContextRecords(db).read("local", {"thread"})
            self.assertEqual(native["responses"]["response"]["tools"]["input_tokens"], 31)
            self.assertEqual(native["items"]["tools"], tools)
            self.assertEqual(native["tools"]["model", tool_key(tools)], 31)
            result = analyze_records(
                [
                    {"type": "response_item", "payload": tools},
                    {"type": "turn_context", "payload": {"model": "model"}},
                    {
                        "type": "token_usage_record",
                        "payload": {
                            "usage": {"input_tokens": 33, "output_tokens": 0, "total_tokens": 33}
                        },
                    },
                ],
                native=native,
            )
            self.assertEqual(result["input"]["delta"], 0)
            self.assertEqual(result["attribution"], "estimated")
            self.assertEqual(ContextRecords(db).read("other", {"thread"})["responses"], {})
            db.close()
