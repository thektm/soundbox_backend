"""Realtime notification delivery helpers for Django Channels.

Database rows remain the source of truth.  These helpers only publish committed
state changes to connected clients; failures are logged and never roll back the
originating application transaction.
"""
from __future__ import annotations

import logging
from typing import Iterable, Mapping, Optional

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction

from .models import Notification

logger = logging.getLogger(__name__)

NOTIFICATION_GROUP_PREFIX = "notifications.user"


def notification_group_name(user_id: int) -> str:
    """Return a Channels-safe, stable group name for one authenticated user."""
    return f"{NOTIFICATION_GROUP_PREFIX}.{int(user_id)}"


def serialize_notification(notification: Notification) -> dict:
    return {
        "id": notification.pk,
        "text": notification.text,
        "text_en": notification.text_en or notification.text,
        "has_read": bool(notification.has_read),
        "created_at": notification.created_at.isoformat(),
    }


def _group_send(user_id: int, event_type: str, payload: Mapping) -> None:
    channel_layer = get_channel_layer()
    if channel_layer is None:
        logger.warning("Notification realtime event skipped: no channel layer configured")
        return

    try:
        async_to_sync(channel_layer.group_send)(
            notification_group_name(user_id),
            {
                "type": "notification_event",
                "event_type": event_type,
                "payload": dict(payload),
            },
        )
    except Exception:
        # Realtime delivery is best-effort. HTTP reconciliation guarantees that
        # the committed database state is recovered after reconnect/focus/Home.
        logger.exception(
            "Failed to publish notification realtime event type=%s user_id=%s",
            event_type,
            user_id,
        )


def publish_notification(notification: Notification) -> None:
    if not notification.user_id or notification.has_read:
        return
    _group_send(
        notification.user_id,
        "notification.created",
        {
            "notification": serialize_notification(notification),
            "has_unread": True,
        },
    )


def publish_notification_by_id(notification_id: int) -> None:
    notification = (
        Notification.objects.filter(pk=notification_id, user_id__isnull=False, has_read=False)
        .only("id", "user_id", "text", "text_en", "has_read", "created_at")
        .first()
    )
    if notification:
        publish_notification(notification)


def publish_notification_ids(notification_ids: Iterable[int]) -> None:
    ids = [int(value) for value in notification_ids if value]
    if not ids:
        return
    notifications = Notification.objects.filter(
        pk__in=ids,
        user_id__isnull=False,
        has_read=False,
    ).only("id", "user_id", "text", "text_en", "has_read", "created_at")
    for notification in notifications.iterator(chunk_size=500):
        publish_notification(notification)


def publish_notification_read(user_id: int, notification_id: int) -> None:
    has_unread = Notification.objects.filter(user_id=user_id, has_read=False).exists()
    _group_send(
        user_id,
        "notification.read",
        {
            "notification_id": int(notification_id),
            "has_unread": has_unread,
        },
    )


def publish_all_notifications_read(
    user_id: int,
    read_through_id: int | None = None,
) -> None:
    has_unread = Notification.objects.filter(user_id=user_id, has_read=False).exists()
    _group_send(
        user_id,
        "notifications.read_all",
        {
            "has_unread": has_unread,
            "read_through_id": int(read_through_id) if read_through_id else None,
        },
    )


def schedule_notification_publish(notification_id: int) -> None:
    """Publish only after the surrounding database transaction commits."""
    transaction.on_commit(lambda: publish_notification_by_id(notification_id))


def schedule_notification_ids_publish(notification_ids: Iterable[int]) -> None:
    ids = tuple(int(value) for value in notification_ids if value)
    if ids:
        transaction.on_commit(lambda: publish_notification_ids(ids))
