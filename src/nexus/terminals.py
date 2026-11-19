"""Machine-level interactive PTYs, retained independently of browser connections."""

from __future__ import annotations

import asyncio
import base64
import errno
import fcntl
import os
import pty
import pwd
import shlex
import signal
import struct
import sys
import termios
import time
from collections import deque
from contextlib import suppress
from uuid import uuid4

from pydantic import BaseModel, Field

from .errors import NexusError
from .ssh import SSH_OPTIONS, ssh_target

HISTORY_BYTES = 2 * 1024 * 1024
PENDING_BYTES = 256 * 1024


class TerminalRequest(BaseModel):
    cwd: str = "~"
    thread_id: str | None = Field(default=None, alias="threadId")
    cols: int = Field(default=80, ge=2, le=1000)
    rows: int = Field(default=24, ge=1, le=500)


class Terminal:
    def __init__(self, machine_id: str, cwd: str, request: TerminalRequest, number: int):
        self.id = uuid4().hex
        self.machine_id = machine_id
        self.cwd = cwd
        self.thread_id = request.thread_id
        self.number = number
        self.name = f"终端 {number}"
        self.created_at = time.time()
        self.cols, self.rows = request.cols, request.rows
        self.process = None
        self.fd = None
        self.reader = False
        self.history = deque()
        self.offset = 0
        self.clients = {}
        self.changed = asyncio.Event()
        self.watcher = None

    def view(self) -> dict:
        code = self.process.returncode
        return {
            "id": self.id,
            "machineId": self.machine_id,
            "cwd": self.cwd,
            "threadId": self.thread_id,
            "name": self.name,
            "createdAt": self.created_at,
            "status": "running" if code is None else "exited",
            "exitCode": code,
            "cols": self.cols,
            "rows": self.rows,
        }

    def notify(self):
        self.changed.set()
        self.changed = asyncio.Event()

    def resize(self, cols: int, rows: int):
        self.cols, self.rows = cols, rows
        if self.fd is not None:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def _reading(self):
        if self.fd is None:
            return
        pause = self.clients and self.offset - min(self.clients.values()) >= PENDING_BYTES
        loop = asyncio.get_running_loop()
        if pause and self.reader:
            loop.remove_reader(self.fd)
            self.reader = False
        elif not pause and not self.reader:
            loop.add_reader(self.fd, self._read)
            self.reader = True

    def _read(self):
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            data = b""
        if not data:
            self._close_fd()
            return
        self.history.append((self.offset, data))
        self.offset += len(data)
        while self.history and self.offset - self.history[0][0] > HISTORY_BYTES:
            self.history.popleft()
        self.notify()
        self._reading()

    def _close_fd(self):
        if self.fd is not None:
            asyncio.get_running_loop().remove_reader(self.fd)
            os.close(self.fd)
            self.fd = None
            self.reader = False

    async def start(self, argv: list[str], env: dict, local: bool):
        master, slave = pty.openpty()
        self.fd = master
        self.resize(self.cols, self.rows)
        try:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "nexus.pty_shell",
                *argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                cwd=self.cwd if local else None,
                env=env,
            )
        except BaseException:
            self._close_fd()
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        self._reading()

    async def watch(self, events):
        await self.process.wait()
        # Drain the final output before reporting the exit.
        while self.fd is not None:
            before = self.offset
            self._read()
            if before == self.offset:
                break
        self._close_fd()
        self.notify()
        await events.publish(self.machine_id, "nexus/terminal/updated", {"terminal": self.view()})

    async def write(self, data: bytes):
        while data and self.fd is not None and self.process.returncode is None:
            try:
                count = os.write(self.fd, data)
                data = data[count:]
            except BlockingIOError:
                ready = asyncio.get_running_loop().create_future()
                fd = self.fd
                asyncio.get_running_loop().add_writer(
                    fd, lambda: ready.done() or ready.set_result(None)
                )
                try:
                    await ready
                finally:
                    asyncio.get_running_loop().remove_writer(fd)

    async def close(self):
        self._close_fd()
        if self.process.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGHUP)
            try:
                await asyncio.wait_for(asyncio.shield(self.process.wait()), 2)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
        if self.watcher:
            await self.watcher

    async def stream(self, websocket, after: int):
        token = uuid4().hex
        first = self.history[0][0] if self.history else self.offset
        reset = not first <= after <= self.offset
        cursor = first if reset else after
        await websocket.send_json(
            {
                "type": "ready",
                "reset": reset,
                "offset": cursor,
                "end": self.offset,
                "cols": self.cols,
                "rows": self.rows,
            }
        )
        self.clients[token] = cursor

        async def output():
            nonlocal cursor
            while True:
                changed = self.changed
                for start, data in list(self.history):
                    end = start + len(data)
                    if end <= cursor:
                        continue
                    await websocket.send_json(
                        {
                            "type": "output",
                            "offset": end,
                            "data": base64.b64encode(data[max(0, cursor - start) :]).decode(),
                        }
                    )
                    cursor = end
                if self.process.returncode is not None:
                    await websocket.send_json({"type": "exit", "exitCode": self.process.returncode})
                    return
                await changed.wait()

        async def input_():
            while True:
                message = await websocket.receive_json()
                kind = message.get("type")
                if kind == "input":
                    await self.write(message["data"].encode())
                elif kind == "input-binary":
                    await self.write(base64.b64decode(message["data"]))
                elif kind == "resize":
                    cols, rows = int(message["cols"]), int(message["rows"])
                    if 2 <= cols <= 1000 and 1 <= rows <= 500:
                        self.resize(cols, rows)
                elif kind == "ack":
                    self.clients[token] = max(
                        self.clients[token], min(int(message["offset"]), cursor)
                    )
                    self._reading()

        tasks = [asyncio.create_task(output()), asyncio.create_task(input_())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.clients.pop(token, None)
            self._reading()


class Terminals:
    def __init__(self, machine, files, events):
        self.machine, self.files, self.events = machine, files, events
        self.items: dict[str, Terminal] = {}
        self.lock = asyncio.Lock()

    def get(self, terminal_id: str) -> Terminal:
        if terminal_id not in self.items:
            raise NexusError("终端已关闭", status_code=404)
        return self.items[terminal_id]

    def list(self) -> list[dict]:
        return [terminal.view() for terminal in self.items.values()]

    async def create(self, machine_id: str, request: TerminalRequest) -> dict:
        async with self.lock:
            machine = self.machine(machine_id)
            if machine.get("demo"):
                raise NexusError("演示机器不支持终端")
            if request.thread_id:
                for terminal in self.items.values():
                    if (
                        terminal.machine_id == machine_id
                        and terminal.thread_id == request.thread_id
                    ):
                        return terminal.view()
            cwd = str(await self.files.resolve_path(machine_id, request.cwd))
            used = {terminal.number for terminal in self.items.values()}
            number = 1
            while number in used:
                number += 1
            terminal = Terminal(machine_id, cwd, request, number)
            remote = machine.get("transport") == "ssh"
            if remote:
                script = f'cd -- {shlex.quote(cwd)} && exec "${{SHELL:-/bin/bash}}" -il'
                argv = [
                    "ssh",
                    "-tt",
                    *SSH_OPTIONS,
                    "-p",
                    str(machine.get("port") or 22),
                    "--",
                    ssh_target(machine),
                    script,
                ]
            else:
                shell = os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell or "/bin/bash"
                argv = [shell, "-il"]
            try:
                await terminal.start(argv, {**os.environ, "TERM": "xterm-256color"}, not remote)
            except OSError as exc:
                raise NexusError(str(exc), status_code=502) from exc
            self.items[terminal.id] = terminal
            terminal.watcher = asyncio.create_task(terminal.watch(self.events))
            await self.events.publish(
                machine_id, "nexus/terminal/updated", {"terminal": terminal.view()}
            )
            return terminal.view()

    async def delete(self, terminal_id: str):
        terminal = self.get(terminal_id)
        # Keep the binding until shutdown finishes so a concurrent open reuses it.
        await terminal.close()
        self.items.pop(terminal_id, None)
        await self.events.publish(
            terminal.machine_id, "nexus/terminal/deleted", {"terminalId": terminal_id}
        )

    async def close(self, machine_id: str | None = None):
        await asyncio.gather(
            *(
                self.delete(terminal.id)
                for terminal in list(self.items.values())
                if machine_id is None or terminal.machine_id == machine_id
            )
        )
