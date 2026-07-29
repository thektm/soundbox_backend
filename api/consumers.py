"""Authenticated per-user notification WebSocket consumer."""
from __future__ import annotations

import logging
import time

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .models import Notification
from .realtime_notifications import notification_group_name

logger = logging.getLogger(__name__)
PUBLIC_SUBPROTOCOL = "sedabox.notifications"
GROUP_REFRESH_INTERVAL_SECONDS = 60


class NotificationConsumer(AsyncJsonWebsocketConsumer):
    """A minimal socket: one authenticated group, tiny JSON events, no DB writes."""

    group_name: str | None = None

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated or not user.is_active:
            await self.close(code=4401)
            return

        if self.channel_layer is None:
            logger.error("Notification socket rejected: channel layer is unavailable")
            await self.close(code=1011)
            return

        self.group_name = notification_group_name(user.pk)
        try:
            await self.channel_layer.group_add(self.group_name, self.channel_name)
        except Exception:
            logger.exception(
                "Notification socket group_add failed user_id=%s", user.pk
            )
            self.group_name = None
            await self.close(code=1011)
            return

        accepted_protocol = (
            PUBLIC_SUBPROTOCOL
            if PUBLIC_SUBPROTOCOL in self.scope.get("subprotocols", ())
            else None
        )
        await self.accept(subprotocol=accepted_protocol)
        self._last_group_refresh = time.monotonic()

        try:
            unread_count = await self._unread_count(user.pk)
        except Exception:
            logger.exception(
                "Notification socket unread-count query failed user_id=%s", user.pk
            )
            await self.close(code=1011)
            return

        await self.send_json(
            {
                "type": "notifications.connected",
                "unread_count": unread_count,
            }
        )

    async def disconnect(self, close_code):
        if self.group_name and self.channel_layer is not None:
            try:
                await self.channel_layer.group_discard(
                    self.group_name, self.channel_name
                )
            except Exception:
                # Redis may already be unavailable during shutdown. The channel
                # and group entries expire automatically; log without masking exit.
                logger.warning(
                    "Notification socket group_discard failed group=%s",
                    self.group_name,
                    exc_info=True,
                )
            finally:
                self.group_name = None

    async def receive_json(self, content, **kwargs):
        # Browser WebSockets do not expose protocol ping frames. This tiny app
        # heartbeat detects dead proxies/mobile-network transitions.
        if isinstance(content, dict) and content.get("type") == "ping":
            # Redis restarts clear ephemeral group membership, and Channels group
            # entries also expire. Refresh periodically without reconnecting or
            # writing to Redis for every 25-second heartbeat.
            if (
                self.group_name
                and self.channel_layer is not None
                and time.monotonic() - getattr(self, "_last_group_refresh", 0)
                >= GROUP_REFRESH_INTERVAL_SECONDS
            ):
                try:
                    await self.channel_layer.group_add(
                        self.group_name, self.channel_name
                    )
                    self._last_group_refresh = time.monotonic()
                except Exception:
                    logger.warning(
                        "Notification socket group refresh failed group=%s",
                        self.group_name,
                        exc_info=True,
                    )
                    await self.close(code=1011)
                    return
            await self.send_json({"type": "pong", "ts": content.get("ts")})

    async def notification_event(self, event):
        event_type = event.get("event_type")
        payload = event.get("payload") or {}
        if event_type not in {
            "notification.created",
            "notification.read",
            "notifications.read_all",
        } or not isinstance(payload, dict):
            return
        await self.send_json({"type": event_type, **payload})

    @database_sync_to_async
    def _unread_count(self, user_id: int) -> int:
        return Notification.objects.filter(user_id=user_id, has_read=False).count()
