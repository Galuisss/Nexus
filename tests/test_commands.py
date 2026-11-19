import sys
import unittest
from unittest.mock import AsyncMock, patch

from nexus.commands import OUTPUT_BYTES_CAP, CommandExecutor
from nexus.errors import NexusError


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.executor = CommandExecutor(lambda _: {"transport": "local"})

    async def test_binary_output_preserves_bytes_beyond_text_limit(self):
        payload = bytes(range(256)) * 5000
        command = [
            sys.executable,
            "-c",
            "import os; os.write(1, bytes(range(256)) * 5000); os.write(2, b'warning')",
        ]
        result = await self.executor.execute("local", command, "/", binary_stdout=True)
        self.assertEqual(result["stdout"], payload)
        self.assertFalse(result["stdoutTruncated"])
        self.assertEqual(result["stderr"], "warning")
        self.assertEqual(result["exitCode"], 0)
        text = await self.executor.execute("local", command, "/")
        self.assertTrue(text["stdoutTruncated"])
        self.assertEqual(text["stdout"], payload[:OUTPUT_BYTES_CAP].decode(errors="replace"))

    async def test_binary_timeout_returns_partial_output(self):
        command = [
            sys.executable,
            "-c",
            "import os, time; os.write(1, bytes([255])); time.sleep(30)",
        ]
        result = await self.executor.execute("local", command, "/", 0.2, binary_stdout=True)
        self.assertTrue(result["timedOut"])
        self.assertEqual(result["stdout"], b"\xff")

    async def test_screenshot_reports_adb_failure_and_timeout(self):
        result = {
            "exitCode": 1,
            "stderr": "adb: device offline",
            "stdout": b"",
            "timedOut": False,
            "stdoutTruncated": False,
        }
        for timed_out, status, message in ((False, 502, "device offline"), (True, 504, "超时")):
            with self.subTest(timed_out=timed_out):
                with patch.object(
                    self.executor,
                    "execute",
                    new_callable=AsyncMock,
                    return_value={**result, "timedOut": timed_out},
                ):
                    with self.assertRaisesRegex(NexusError, message) as caught:
                        await self.executor.screenshot("phone")
                self.assertEqual(caught.exception.status_code, status)
