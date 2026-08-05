"""Authenticated, role-scoped notification WebSocket consumer."""
from __future__ import annotations

import logging
import time
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .models import Notification, User
from .realtime_notifications import (
    normalize_notification_role,
    notification_group_name,
)

logger = logging.getLogger(__name__)
PUBLIC_SUBPROTOCOL = "sedabox.notifications"
GROUP_REFRESH_INTERVAL_SECONDS = 60


class NotificationConsumer(AsyncJsonWebsocketConsumer):
    """One authenticated account + one explicit app role per connection."""

    group_name: str | None = None
    recipient_role: str | None = None

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated or not user.is_active:
            origin = next((
                value.decode("latin-1", errors="ignore")
                for key, value in self.scope.get("headers", ())
                if key.lower() == b"origin"
            ), "")
            logger.warning(
                "Notification socket rejected: auth_status=%s token_source=%s origin=%s path=%s",
                self.scope.get("ws_auth_status", "unknown"),
                self.scope.get("ws_token_source", "unknown"),
                origin,
                self.scope.get("path", ""),
            )
            await self.close(code=4401)
            return

        query = parse_qs(
            self.scope.get("query_string", b"").decode("utf-8", errors="ignore")
        )
        self.recipient_role = normalize_notification_role(
            (query.get("role") or [None])[0]
        )
        if not self.recipient_role:
            logger.warning("Notification socket rejected: missing/invalid role user_id=%s", user.pk)
            await self.close(code=4400)
            return

        if not await self._can_use_role(user.pk, self.recipient_role):
            logger.warning(
                "Notification socket role rejected: user_id=%s role=%s",
                user.pk,
                self.recipient_role,
            )
            await self.close(code=4403)
            return

        if self.channel_layer is None:
            logger.error("Notification socket rejected: channel layer is unavailable")
            await self.close(code=1011)
            return

        self.group_name = notification_group_name(user.pk, self.recipient_role)
        try:
            await self.channel_layer.group_add(self.group_name, self.channel_name)
        except Exception:
            logger.exception(
                "Notification socket group_add failed user_id=%s role=%s",
                user.pk,
                self.recipient_role,
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
            unread_count = await self._unread_count(user.pk, self.recipient_role)
        except Exception:
            logger.exception(
                "Notification unread query failed user_id=%s role=%s",
                user.pk,
                self.recipient_role,
            )
            await self.close(code=1011)
            return

        await self.send_json({
            "type": "notifications.connected",
            "recipient_role": self.recipient_role,
            "unread_count": unread_count,
        })

    async def disconnect(self, close_code):
        if self.group_name and self.channel_layer is not None:
            try:
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
            except Exception:
                logger.warning(
                    "Notification socket group_discard failed group=%s",
                    self.group_name,
                    exc_info=True,
                )
            finally:
                self.group_name = None

    async def receive_json(self, content, **kwargs):
        if isinstance(content, dict) and content.get("type") == "ping":
            if (
                self.group_name
                and self.channel_layer is not None
                and time.monotonic() - getattr(self, "_last_group_refresh", 0)
                >= GROUP_REFRESH_INTERVAL_SECONDS
            ):
                try:
                    await self.channel_layer.group_add(self.group_name, self.channel_name)
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
        if payload.get("recipient_role") != self.recipient_role:
            logger.error(
                "Blocked cross-role notification event socket_role=%s event_role=%s",
                self.recipient_role,
                payload.get("recipient_role"),
            )
            return
        await self.send_json({"type": event_type, **payload})

    @database_sync_to_async
    def _can_use_role(self, user_id: int, recipient_role: str) -> bool:
        user = User.objects.only("roles").filter(pk=user_id, is_active=True).first()
        return bool(user and recipient_role in (user.roles or []))

    @database_sync_to_async
    def _unread_count(self, user_id: int, recipient_role: str) -> int:
        return Notification.objects.filter(
            user_id=user_id,
            recipient_role=recipient_role,
            has_read=False,
        ).count()
