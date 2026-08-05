"""Role-scoped realtime notification delivery helpers for Django Channels."""
from __future__ import annotations

import logging
from typing import Iterable, Mapping

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction

from .models import Notification

logger = logging.getLogger(__name__)
NOTIFICATION_GROUP_PREFIX = "notifications.account"
VALID_NOTIFICATION_ROLES = frozenset({
    Notification.ROLE_AUDIENCE,
    Notification.ROLE_ARTIST,
})


def normalize_notification_role(value: object) -> str | None:
    role = str(value or "").strip().lower()
    return role if role in VALID_NOTIFICATION_ROLES else None


def notification_group_name(user_id: int, recipient_role: str) -> str:
    role = normalize_notification_role(recipient_role)
    if not role:
        raise ValueError("A valid notification recipient role is required")
    return f"{NOTIFICATION_GROUP_PREFIX}.{int(user_id)}.{role}"


def notification_owner_user_id(notification: Notification) -> int | None:
    return notification.user_id


def serialize_notification(notification: Notification) -> dict:
    return {
        "id": notification.pk,
        "recipient_role": notification.recipient_role,
        "text": notification.text,
        "text_en": notification.text_en or notification.text,
        "has_read": bool(notification.has_read),
        "created_at": notification.created_at.isoformat(),
    }


def _group_send(
    user_id: int,
    recipient_role: str,
    event_type: str,
    payload: Mapping,
) -> None:
    channel_layer = get_channel_layer()
    if channel_layer is None:
        logger.warning("Notification realtime event skipped: no channel layer configured")
        return

    role = normalize_notification_role(recipient_role)
    if not role:
        logger.error("Notification realtime event skipped: invalid role %r", recipient_role)
        return

    try:
        async_to_sync(channel_layer.group_send)(
            notification_group_name(user_id, role),
            {
                "type": "notification_event",
                "event_type": event_type,
                "payload": {**dict(payload), "recipient_role": role},
            },
        )
    except Exception:
        logger.exception(
            "Failed to publish notification event type=%s user_id=%s role=%s",
            event_type,
            user_id,
            role,
        )


def publish_notification(notification: Notification) -> None:
    if notification.has_read:
        return
    user_id = notification_owner_user_id(notification)
    role = normalize_notification_role(notification.recipient_role)
    if not user_id or not role:
        return
    _group_send(
        user_id,
        role,
        "notification.created",
        {
            "notification": serialize_notification(notification),
            "has_unread": True,
        },
    )


def publish_notification_by_id(notification_id: int) -> None:
    notification = (
        Notification.objects.filter(pk=notification_id, has_read=False)
        .only(
            "id", "user_id", "recipient_role", "text", "text_en",
            "has_read", "created_at",
        )
        .first()
    )
    if notification:
        publish_notification(notification)


def publish_notification_ids(notification_ids: Iterable[int]) -> None:
    ids = [int(value) for value in notification_ids if value]
    if not ids:
        return
    notifications = (
        Notification.objects.filter(pk__in=ids, has_read=False)
        .only(
            "id", "user_id", "recipient_role", "text", "text_en",
            "has_read", "created_at",
        )
    )
    for notification in notifications.iterator(chunk_size=500):
        publish_notification(notification)


def _has_unread(user_id: int, recipient_role: str) -> bool:
    return Notification.objects.filter(
        user_id=user_id,
        recipient_role=recipient_role,
        has_read=False,
    ).exists()


def publish_notification_read(
    user_id: int,
    recipient_role: str,
    notification_id: int,
) -> None:
    role = normalize_notification_role(recipient_role)
    if not role:
        return
    _group_send(
        user_id,
        role,
        "notification.read",
        {
            "notification_id": int(notification_id),
            "has_unread": _has_unread(user_id, role),
        },
    )


def publish_all_notifications_read(
    user_id: int,
    recipient_role: str,
    read_through_id: int | None = None,
) -> None:
    role = normalize_notification_role(recipient_role)
    if not role:
        return
    _group_send(
        user_id,
        role,
        "notifications.read_all",
        {
            "has_unread": _has_unread(user_id, role),
            "read_through_id": int(read_through_id) if read_through_id else None,
        },
    )


def schedule_notification_publish(notification_id: int) -> None:
    transaction.on_commit(lambda: publish_notification_by_id(notification_id))


def schedule_notification_ids_publish(notification_ids: Iterable[int]) -> None:
    ids = tuple(int(value) for value in notification_ids if value)
    if ids:
        transaction.on_commit(lambda: publish_notification_ids(ids))
