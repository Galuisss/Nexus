"""Machine connections, retry scheduling, and transport ownership."""

import asyncio
from collections import defaultdict

from .errors import NexusError
from .runtime import RuntimeConnection

CONNECT_TIMEOUT = 30


class MachineConnections:
    def __init__(self, service):
        self.service = service
        self.connections: dict[str, RuntimeConnection] = {}
        self.status: dict[str, dict] = {}
        self.wanted: set[str] = set()
        self.reconnect_tasks: dict[str, asyncio.Task] = {}
        self.connect_tasks: dict[str, asyncio.Task] = {}
        self.locks = defaultdict(asyncio.Lock)

    async def connect(self, machine_id: str) -> dict:
        self.service.machine(machine_id)
        self.wanted.add(machine_id)
        if (
            self.status.get(machine_id, {}).get("status") == "connected"
            and machine_id in self.connections
        ):
            return self.service._machine_view(self.service.machine(machine_id))
        task = self.connect_tasks.get(machine_id)
        if task is None:
            task = asyncio.create_task(
                self._connect_with_timeout(machine_id), name=f"nexus-connect-{machine_id}"
            )
            self.connect_tasks[machine_id] = task
            task.add_done_callback(lambda done: self._connect_done(machine_id, done))
        # Cancelling a caller leaves the shared connection attempt running.
        await asyncio.wait({task})
        return task.result()

    def _connect_done(self, machine_id: str, task: asyncio.Task) -> None:
        if self.connect_tasks.get(machine_id) is task:
            self.connect_tasks.pop(machine_id)
        if not task.cancelled():
            task.exception()

    async def _connect_with_timeout(self, machine_id: str) -> dict:
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                return await self._connect(machine_id)
        except TimeoutError as exc:
            error = NexusError("连接机器超时", status_code=504)
            self.status[machine_id] = {"status": "reconnecting", "error": str(error)}
            await self.service._machine_event(machine_id)
            self._schedule_reconnect(machine_id)
            raise error from exc

    def connection(self, machine_id: str) -> RuntimeConnection:
        state = self.status.get(machine_id, {})
        if state.get("status") != "connected" or machine_id not in self.connections:
            message = (
                "机器正在连接"
                if state.get("status") in {"connecting", "reconnecting"}
                else "机器未连接"
            )
            raise NexusError(state.get("error") or message, status_code=503)
        return self.connections[machine_id]

    async def _connect(self, machine_id: str) -> dict:
        machine = self.service.machine(machine_id)
        async with self.locks[machine_id]:
            if self.service.demo:
                self.status[machine_id] = {"status": "connected", "version": "0.169.0"}
                self.service.search_sync.wake(machine_id)
                await self.service._machine_event(machine_id)
                return self.service._machine_view(machine)
            if machine_id in self.connections:
                return self.service._machine_view(machine)
            self.status[machine_id] = {"status": "connecting"}
            await self.service._machine_event(machine_id)
            connection = RuntimeConnection(
                machine,
                lambda method, params: self.service._enqueue(
                    "notification", machine_id, connection, (method, params)
                ),
                lambda message: self.service._enqueue("request", machine_id, connection, message),
                lambda error: self.service._enqueue("disconnected", machine_id, connection, error),
                nexus_url=self.service.nexus_url,
            )
            self.connections[machine_id] = connection
            try:
                await connection.connect()
                await self.service._restore_observers(machine_id, connection)
            except (Exception, asyncio.CancelledError) as exc:
                self.connections.pop(machine_id, None)
                await connection.close()
                await self.service._clear_requests(machine_id)
                self.status[machine_id] = {"status": "reconnecting", "error": str(exc)}
                await self.service._machine_event(machine_id)
                if not isinstance(exc, asyncio.CancelledError):
                    self._schedule_reconnect(machine_id)
                raise
            self.status[machine_id] = {"status": "connected", "version": connection.version}
            self.service._start_project_index(machine_id)
            self.service.search_sync.wake(machine_id)
            self.service.history_watch.start(machine_id)
            await self.service._machine_event(machine_id)
            return self.service._machine_view(machine)

    def _schedule_reconnect(self, machine_id: str) -> None:
        if (
            self.service.closing
            or machine_id not in self.wanted
            or machine_id in self.reconnect_tasks
        ):
            return
        self.reconnect_tasks[machine_id] = asyncio.create_task(
            self._reconnect(machine_id),
            name=f"nexus-reconnect-{machine_id}",
        )

    async def _reconnect(self, machine_id: str) -> None:
        delay = 1
        try:
            while machine_id in self.wanted and not self.service.closing:
                await asyncio.sleep(delay)
                try:
                    await self.connect(machine_id)
                    return
                except Exception:
                    delay = min(delay * 2, 30)
        finally:
            self.reconnect_tasks.pop(machine_id, None)

    async def disconnect(self, machine_id: str) -> dict:
        return await self._disconnect(machine_id)

    async def reconnect(self, machine_id: str) -> dict:
        await self._disconnect(machine_id, reconnecting=True)
        return await self.connect(machine_id)

    async def _disconnect(self, machine_id: str, *, reconnecting: bool = False) -> dict:
        machine = self.service.machine(machine_id)
        self.wanted.discard(machine_id)
        await self.service.history_watch.stop(machine_id)
        await self.service.search_sync.stop(machine_id)
        project_task = self.service.project_tasks.pop(machine_id, None)
        if project_task:
            project_task.cancel()
            await asyncio.gather(project_task, return_exceptions=True)
        retry = self.reconnect_tasks.pop(machine_id, None)
        if retry:
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)
        attempt = self.connect_tasks.pop(machine_id, None)
        if attempt:
            attempt.cancel()
            await asyncio.gather(attempt, return_exceptions=True)
        async with self.locks[machine_id]:
            sessions = self.service.sessions
            if reconnecting:
                sessions.recovering_turns.update(
                    {
                        key: turn
                        for key, turn in sessions.active_turns.items()
                        if key[0] == machine_id
                    }
                )
            else:
                sessions.recovering_turns = {
                    key: turn
                    for key, turn in sessions.recovering_turns.items()
                    if key[0] != machine_id
                }
            connection = self.connections.pop(machine_id, None)
            if connection:
                await connection.close()
            await self.service._clear_requests(machine_id)
            self.status[machine_id] = {"status": "reconnecting" if reconnecting else "disconnected"}
            await self.service._machine_event(machine_id)
            return self.service._machine_view(machine)

    async def disconnected(self, machine_id: str, source: RuntimeConnection, error: str) -> None:
        async with self.locks[machine_id]:
            if self.connections.get(machine_id) is not source:
                return
            sessions = self.service.sessions
            sessions.recovering_turns.update(
                {key: turn for key, turn in sessions.active_turns.items() if key[0] == machine_id}
            )
            self.status[machine_id] = {"status": "reconnecting", "error": error}
            await self.service.history_watch.stop(machine_id)
            await self.service.search_sync.stop(machine_id)
            self.connections.pop(machine_id)
            await source.close()
            await self.service._clear_requests(machine_id)
            await self.service._machine_event(machine_id)
            self._schedule_reconnect(machine_id)

    async def close(self) -> None:
        tasks = [*self.reconnect_tasks.values(), *self.connect_tasks.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        connections = list(self.connections.values())
        self.connections.clear()
        await asyncio.gather(*(connection.close() for connection in connections))
