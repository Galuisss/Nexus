"""Ordered, replayable notifications shared by Web, HTTP and MCP clients."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from typing import AsyncIterator
from uuid import uuid4


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventBroker:
    def __init__(self, capacity: int = 2000):
        self.history: deque[dict] = deque(maxlen=capacity)
        self.sequence = 0
        self.epoch = uuid4().hex
        self.changed = asyncio.Condition()
        self.closed = False

    def cursor(self) -> dict:
        return {"epoch": self.epoch, "id": self.sequence}

    async def publish(self, machine_id: str, method: str, params: dict, **extra) -> dict:
        async with self.changed:
            self.sequence += 1
            event = {
                "id": self.sequence,
                "epoch": self.epoch,
                "machineId": machine_id,
                "method": method,
                "params": params,
                "time": now(),
                **extra,
            }
            self.history.append(event)
            self.changed.notify_all()
            return event

    async def close(self) -> None:
        async with self.changed:
            self.closed = True
            self.changed.notify_all()

    async def subscribe(
        self,
        after: int | None = None,
        machine_id: str | None = None,
        thread_id: str | None = None,
        epoch: str | None = None,
    ) -> AsyncIterator[dict]:
        cursor = self.sequence if after is None else after
        while not self.closed:
            async with self.changed:
                if (
                    (epoch is not None and epoch != self.epoch)
                    or cursor > self.sequence
                    or (self.history and cursor < self.history[0]["id"] - 1)
                ):
                    events = [
                        {
                            "id": self.sequence,
                            "epoch": self.epoch,
                            "machineId": machine_id or "",
                            "method": "nexus/resync",
                            "params": {},
                            "time": now(),
                        }
                    ]
                    cursor = self.sequence
                    epoch = self.epoch
                else:
                    events = [event for event in self.history if event["id"] > cursor]
                    if events:
                        cursor = events[-1]["id"]
                    else:
                        await self.changed.wait()
                        continue
            for event in events:
                if event["method"] == "nexus/resync":
                    yield event
                    continue
                if machine_id and event["machineId"] != machine_id:
                    continue
                params = event["params"]
                event_thread = params.get("threadId") or params.get("thread", {}).get("id")
                if thread_id and event_thread != thread_id:
                    continue
                yield event
