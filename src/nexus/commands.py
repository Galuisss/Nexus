"""Machine commands and Android tools, independent of Codex."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shlex
import signal
from contextlib import suppress
from importlib.resources import files
from pathlib import PurePosixPath

from pydantic import BaseModel, Field

from .errors import NexusError
from .ssh import SSH_OPTIONS, ssh_target

OUTPUT_BYTES_CAP = 1024 * 1024
IMAGE_BYTES_CAP = 16 * 1024 * 1024
CLEANUP_TIMEOUT = 5

# Keep SSH stdin open until completion. EOF stops the command's process group;
# the timer also bounds execution if the network disappears.
REMOTE_SCRIPT = """
cd -- "$1" || exit
deadline=$2
shift 2
exec 3>&2 2>/dev/null
set -m
"$@" </dev/null 2>&3 3>&- &
command_pid=$!
(cat >/dev/null; kill -KILL -- "-$command_pid" 2>/dev/null) <&0 >/dev/null 2>&1 &
input_pid=$!
(sleep "$deadline"; kill -KILL -- "-$command_pid" 2>/dev/null) >/dev/null 2>&1 &
timer_pid=$!
trap 'kill -KILL -- "-$command_pid" "-$input_pid" "-$timer_pid" 2>/dev/null || true' EXIT
trap 'exit 129' HUP
trap 'exit 143' TERM
set +m
wait "$command_pid" 2>/dev/null
exit $?
"""

SCREENSHOT_SCRIPT = """
set -e
directory="${XDG_CACHE_HOME:-$HOME/.cache}/nexus/screenshots"
mkdir -p -- "$directory"
path=$(mktemp "$directory/screenshot-XXXXXXXX.png")
trap 'rm -f -- "$path"' EXIT
"$@" > "$path"
printf '%s\\0' "$path"
cat -- "$path"
trap - EXIT
"""

PHONE_NUMBERS_SCRIPT = """
set -e
directory=$(mktemp -d /data/local/tmp/nexus-phone-numbers-XXXXXXXX)
trap 'rm -rf -- "$directory"' EXIT
printf '%s' "$1" | base64 -d > "$directory/helper.dex"
chmod 444 "$directory/helper.dex"
CLASSPATH="$directory/helper.dex" app_process /system/bin NexusPhoneNumbers
"""


class CommandRequest(BaseModel):
    command: list[str] = Field(min_length=1)
    cwd: str
    timeout_seconds: float = Field(default=60, alias="timeoutSeconds", gt=0)


class ScreenshotRequest(BaseModel):
    serial: str | None = None
    include_path: bool = Field(default=True, alias="includePath")
    timeout_seconds: float = Field(default=15, alias="timeoutSeconds", gt=0)


async def _read_output(
    stream: asyncio.StreamReader, *, binary: bool = False
) -> tuple[str | bytes, bool]:
    output = bytearray()
    truncated = False
    limit = IMAGE_BYTES_CAP if binary else OUTPUT_BYTES_CAP
    while chunk := await stream.read(65536):
        remaining = limit - len(output)
        output.extend(chunk[:remaining])
        truncated |= len(chunk) > remaining
    return (bytes(output) if binary else output.decode(errors="replace")), truncated


async def _stop(process: asyncio.subprocess.Process, remote: bool) -> None:
    if remote:
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), CLEANUP_TIMEOUT)
            return
        except TimeoutError:
            pass
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


class CommandExecutor:
    def __init__(self, machine):
        self.machine = machine

    async def phone_numbers(
        self, machine_id: str, serial: str | None = None, timeout_seconds: float = 15
    ) -> dict:
        dex = files("nexus").joinpath("android/phone_numbers.dex").read_bytes()
        command = ["adb"]
        if serial:
            command.extend(["-s", serial])
        command.extend(
            [
                "shell",
                shlex.join(
                    [
                        "sh",
                        "-c",
                        PHONE_NUMBERS_SCRIPT,
                        "nexus-phone-numbers",
                        base64.b64encode(dex).decode("ascii"),
                    ]
                ),
            ]
        )
        result = await self.execute(machine_id, command, "/", timeout_seconds)
        if result["timedOut"]:
            raise NexusError("ADB 读取手机号超时", status_code=504)
        if result["exitCode"] != 0:
            raise NexusError(result["stderr"].strip() or "ADB 读取手机号失败", status_code=502)
        if result["stdoutTruncated"]:
            raise NexusError("手机号查询结果超过 1 MiB", status_code=502)
        try:
            return json.loads(result["stdout"])
        except json.JSONDecodeError as exc:
            raise NexusError("ADB 未返回手机号查询结果", status_code=502) from exc

    async def screenshot(
        self,
        machine_id: str,
        serial: str | None = None,
        timeout_seconds: float = 15,
        *,
        include_path: bool = True,
    ) -> tuple[bytes, str | None]:
        command = ["adb"]
        if serial:
            command.extend(["-s", serial])
        command.extend(["exec-out", "screencap", "-p"])
        if include_path:
            command = ["bash", "-c", SCREENSHOT_SCRIPT, "nexus-screenshot", *command]
        result = await self.execute(machine_id, command, "/", timeout_seconds, binary_stdout=True)
        if result["timedOut"]:
            raise NexusError("ADB 截图超时", status_code=504)
        if result["exitCode"] != 0:
            raise NexusError(result["stderr"].strip() or "ADB 截图失败", status_code=502)
        if result["stdoutTruncated"]:
            raise NexusError("截图超过 16 MiB", status_code=502)
        png = result["stdout"]
        path = None
        if include_path:
            encoded_path, _, png = png.partition(b"\0")
            path = encoded_path.decode()
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise NexusError("ADB 未返回 PNG 截图", status_code=502)
        return png, path

    async def execute(
        self,
        machine_id: str,
        command: list[str],
        cwd: str,
        timeout_seconds: float = 60,
        *,
        binary_stdout: bool = False,
    ) -> dict:
        machine = self.machine(machine_id)
        if machine.get("demo"):
            raise NexusError("演示机器不支持命令执行")
        if not PurePosixPath(cwd).is_absolute():
            raise NexusError("请提供目标机器上的绝对工作目录")
        remote = machine.get("transport") == "ssh"
        if remote:
            script = shlex.join(
                [
                    "bash",
                    "-lc",
                    REMOTE_SCRIPT,
                    "nexus-command",
                    cwd,
                    str(timeout_seconds + CLEANUP_TIMEOUT),
                    *command,
                ]
            )
            argv = [
                "ssh",
                "-T",
                *SSH_OPTIONS,
                "-p",
                str(machine.get("port") or 22),
                "--",
                ssh_target(machine),
                script,
            ]
        else:
            argv = command
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=None if remote else cwd,
                start_new_session=True,
                stdin=asyncio.subprocess.PIPE if remote else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            raise NexusError(str(exc), status_code=502) from exc

        async def collect():
            stdout, stderr, _ = await asyncio.gather(
                _read_output(process.stdout, binary=binary_stdout),
                _read_output(process.stderr),
                process.wait(),
            )
            return {
                "exitCode": process.returncode,
                "stdout": stdout[0],
                "stderr": stderr[0],
                "stdoutTruncated": stdout[1],
                "stderrTruncated": stderr[1],
            }

        capture = asyncio.create_task(collect())
        timed_out = False
        try:
            result = await asyncio.wait_for(asyncio.shield(capture), timeout_seconds)
        except TimeoutError:
            timed_out = True
            await _stop(process, remote)
            result = await capture
        except asyncio.CancelledError:
            await _stop(process, remote)
            await capture
            raise
        finally:
            if process.stdin:
                process.stdin.close()
        return {**result, "timedOut": timed_out}
