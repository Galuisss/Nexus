"""Persistent Web Push subscriptions and task completion delivery."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from urllib.parse import quote, urlsplit

from aiohttp import ClientError, ClientSession
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid
from pywebpush import WebPushException, webpush_async

from .errors import NexusError
from .store import Store

logger = logging.getLogger(__name__)


class PushNotifications:
    def __init__(self, store: Store):
        self.store = store
        self.key_path = store.directory / "push-private.pem"
        self.subscriptions = store.data.setdefault("pushSubscriptions", {})
        self.tasks: set[asyncio.Task] = set()
        self.completed: dict[str, None] = {}

    def public_key(self) -> str:
        if self.key_path.exists():
            vapid = Vapid.from_file(str(self.key_path))
        else:
            vapid = Vapid()
            vapid.generate_keys()
            vapid.save_key(str(self.key_path))
            self.key_path.chmod(0o600)
        raw = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def subscribe(self, subscription: dict, subject: str) -> None:
        if urlsplit(subscription.get("endpoint", "")).scheme != "https" or not all(
            subscription.get("keys", {}).get(key) for key in ("p256dh", "auth")
        ):
            raise NexusError("无效的通知订阅")
        self.subscriptions[subscription["endpoint"]] = {
            "subscription": subscription,
            "subject": subject,
        }
        self.store.save()

    def unsubscribe(self, endpoint: str) -> None:
        if self.subscriptions.pop(endpoint, None) is not None:
            self.store.save()

    def complete(
        self, machine_id: str, machine_name: str, thread_id: str, title: str, turn: dict
    ) -> None:
        if turn.get("status") != "completed" or not self.subscriptions:
            return
        tag = f"{machine_id}/{thread_id}/{turn['id']}"
        if tag in self.completed:
            return
        self.completed[tag] = None
        if len(self.completed) > 1000:
            self.completed.pop(next(iter(self.completed)))
        payload = {
            "title": "任务已完成",
            "body": f"{machine_name} · {title or 'Codex 会话'}",
            "tag": tag,
            "url": f"/#{quote(machine_id, safe='')}/{quote(thread_id, safe='')}",
        }
        for endpoint in list(self.subscriptions):
            task = asyncio.create_task(self.send(endpoint, payload), name="nexus-push")
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            task.add_done_callback(self._sent)

    @staticmethod
    def _sent(task: asyncio.Task) -> None:
        if not task.cancelled() and (error := task.exception()):
            logger.warning("Completion notification failed: %s", error)

    async def send(self, endpoint: str, payload: dict) -> None:
        entry = self.subscriptions.get(endpoint)
        if entry is None:
            raise NexusError("此设备尚未开启完成通知", status_code=404)
        try:
            async with ClientSession(trust_env=True) as session:
                response = await webpush_async(
                    entry["subscription"],
                    data=json.dumps(payload, ensure_ascii=False),
                    vapid_private_key=str(self.key_path),
                    vapid_claims={"sub": entry["subject"]},
                    ttl=3600,
                    timeout=15,
                    aiohttp_session=session,
                    headers={"Urgency": "high"},
                )
                logger.info(
                    "Push accepted by %s (HTTP %s)", urlsplit(endpoint).hostname, response.status
                )
        except WebPushException as exc:
            logger.warning(
                "Push rejected by %s (HTTP %s)",
                urlsplit(endpoint).hostname,
                exc.response.status if exc.response is not None else "unknown",
            )
            if exc.response is not None and exc.response.status in {404, 410}:
                self.unsubscribe(endpoint)
            raise NexusError("通知投递失败，请检查设备通知设置后重新开启", status_code=502) from exc
        except (ClientError, TimeoutError) as exc:
            raise NexusError("暂时无法连接通知推送服务", status_code=502) from exc

    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
