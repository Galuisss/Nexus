"""Exercise the pinned SDK adapter over a local, scripted JSONL transport."""

import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from unittest.mock import patch

from openai_codex.client import CodexConfig
from openai_codex.errors import TransportClosedError

from nexus.runtime import InteractiveCodexClient, RpcResult

SERVER = """
import json, sys

def send(message):
    print(json.dumps(message), flush=True)

approved = False
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        send({"id": message["id"], "result": {"userAgent": "codex/0.169.0", "serverInfo": {"name": "fixture", "version": "0.169.0"}}})
    elif method == "test/request":
        send({"id": 42, "method": "item/commandExecution/requestApproval", "params": {"threadId": "thread", "turnId": "turn", "command": "pwd"}})
        send({"method": "item/agentMessage/delta", "params": {"threadId": "thread", "turnId": "turn", "itemId": "item", "delta": "hello", "futureField": {"kept": True}}})
        send({"id": message["id"], "result": {"readerContinued": True}})
    elif message.get("id") == 42:
        approved = message["result"] == {"decision": "accept"}
    elif method == "test/approved":
        send({"id": message["id"], "result": {"approved": approved}})
    elif method == "test/disconnect":
        sys.exit(0)
    elif method == "test/snapshot":
        send({"method": "item/agentMessage/delta", "params": {"delta": "before"}})
        send({"id": message["id"], "result": {"text": "before"}})
        send({"method": "item/agentMessage/delta", "params": {"delta": "after"}})
"""


class RuntimeContractTests(unittest.TestCase):
    def test_snapshot_response_and_notifications_share_transport_order(self):
        seen = Queue()
        client = InteractiveCodexClient(
            CodexConfig(launch_args_override=(sys.executable, "-u", "-c", SERVER)),
            lambda request: None,
            on_notification=lambda method, params: seen.put(params["delta"]),
        )
        client.start()
        process = client._proc
        try:
            client.initialize()
            client.request_ordered(
                "test/snapshot", {}, lambda result: seen.put(result["text"] + " snapshot")
            )
            self.assertEqual(
                [seen.get(timeout=3) for _ in range(3)], ["before", "before snapshot", "after"]
            )
        finally:
            client.close()
            process.stdout.close()
            process.stderr.close()

    def test_silent_rpc_times_out_and_a_later_request_can_complete(self):
        timeouts = []
        client = InteractiveCodexClient(CodexConfig(), lambda request: None, timeouts.append)
        with patch.object(client, "_write_message"), patch("nexus.runtime.RPC_TIMEOUT", 0.02):
            with self.assertRaises(TimeoutError):
                client.request("test/silent", {}, response_model=RpcResult)
        self.assertFalse(client._router._response_waiters)
        self.assertEqual(timeouts, ["Codex RPC test/silent 超时"])

        def reply(message):
            client._router.route_response({"id": message["id"], "result": {"ready": True}})

        with patch.object(client, "_write_message", side_effect=reply):
            result = client.request("test/ready", {}, response_model=RpcResult)
        self.assertTrue(result.root["ready"])

    def test_pending_approval_does_not_block_rpc_or_turn_notifications(self):
        requests = Queue()
        client = InteractiveCodexClient(
            CodexConfig(launch_args_override=(sys.executable, "-u", "-c", SERVER)),
            requests.put,
        )
        client.start()
        process = client._proc
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            pool.submit(client.initialize).result(timeout=3)
            result = pool.submit(
                client.request, "test/request", {}, response_model=RpcResult
            ).result(timeout=3)
            self.assertTrue(result.root["readerContinued"])
            request = requests.get(timeout=3)
            self.assertEqual(request["id"], 42)
            event = pool.submit(client.next_notification).result(timeout=3)
            self.assertEqual(event.method, "item/agentMessage/delta")
            self.assertEqual(event.payload.params["futureField"], {"kept": True})
            client.respond(request["id"], {"decision": "accept"})
            result = pool.submit(
                client.request, "test/approved", {}, response_model=RpcResult
            ).result(timeout=3)
            self.assertTrue(result.root["approved"])
            closed = pool.submit(client.request, "test/disconnect", {}, response_model=RpcResult)
            with self.assertRaises(TransportClosedError):
                closed.result(timeout=3)
        finally:
            client.close()
            process.stdout.close()
            process.stderr.close()
            pool.shutdown(wait=True)
