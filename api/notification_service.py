"""Central, role-safe notification creation and preference enforcement."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Iterable, Optional

from django.db import transaction
from django.utils import timezone

from .models import Artist, Notification, NotificationSetting, User
from .realtime_notifications import (
    schedule_notification_ids_publish,
    schedule_notification_publish,
)

logger = logging.getLogger(__name__)

EVENT_NEW_SONG = "new_song_followed_artists"
EVENT_NEW_ALBUM = "new_album_followed_artists"
EVENT_NEW_PLAYLIST = "new_playlist"
EVENT_NEW_LIKE = "new_likes"
EVENT_NEW_FOLLOWER = "new_follower"
EVENT_SYSTEM = "system_notifications"

EVENT_SETTING_FIELDS = {
    EVENT_NEW_SONG: "new_song_followed_artists",
    EVENT_NEW_ALBUM: "new_album_followed_artists",
    EVENT_NEW_PLAYLIST: "new_playlist",
    EVENT_NEW_LIKE: "new_likes",
    EVENT_NEW_FOLLOWER: "new_follower",
    EVENT_SYSTEM: "system_notifications",
}

DEFAULT_DEDUPE_WINDOW = timedelta(minutes=2)


def _setting_field(event: str) -> str:
    try:
        return EVENT_SETTING_FIELDS[event]
    except KeyError as exc:
        raise ValueError(f"Unsupported notification event: {event}") from exc


def _get_or_create_setting(user_id: int) -> NotificationSetting:
    setting, _ = NotificationSetting.objects.get_or_create(user_id=user_id)
    return setting


def notification_enabled(user: Optional[User], event: str) -> bool:
    """Return the audience-app preference for one event."""
    if not user or not getattr(user, "pk", None):
        return False
    field = _setting_field(event)
    if not User.objects.filter(pk=user.pk, is_active=True).exists():
        return False
    setting = _get_or_create_setting(user.pk)
    return bool(getattr(setting, field, False))


def _refresh_or_create_notification(
    *,
    user_id: int,
    recipient_role: str,
    text: str,
    text_en: str,
    artist_id: int | None,
    dedupe_window: timedelta,
) -> Notification:
    now = timezone.now()
    cutoff = now - dedupe_window
    existing = (
        Notification.objects.select_for_update()
        .filter(
            user_id=user_id,
            recipient_role=recipient_role,
            text=text,
            created_at__gte=cutoff,
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if existing:
        should_publish = bool(existing.has_read)
        updates = {
            "has_read": False,
            "created_at": now,
            "text_en": text_en or existing.text_en or text,
        }
        if artist_id and existing.artist_id != artist_id:
            updates["artist_id"] = artist_id
        Notification.objects.filter(pk=existing.pk).update(**updates)
        existing.has_read = False
        existing.created_at = now
        existing.text_en = updates["text_en"]
        if artist_id:
            existing.artist_id = artist_id
        if should_publish:
            schedule_notification_publish(existing.pk)
        return existing

    return Notification.objects.create(
        user_id=user_id,
        artist_id=artist_id,
        recipient_role=recipient_role,
        text=text,
        text_en=text_en or text,
        has_read=False,
    )


def send_user_notification(
    *,
    user: Optional[User],
    event: str,
    text: str,
    text_en: str = "",
    dedupe_window: timedelta = DEFAULT_DEDUPE_WINDOW,
) -> Optional[Notification]:
    """Create one audience-role notification, respecting audience settings."""
    if not user or not getattr(user, "pk", None) or not text:
        return None

    field = _setting_field(event)
    with transaction.atomic():
        locked_user = (
            User.objects.select_for_update()
            .only("id", "is_active")
            .filter(pk=user.pk)
            .first()
        )
        if not locked_user or not locked_user.is_active:
            return None

        setting, _ = NotificationSetting.objects.get_or_create(user_id=locked_user.pk)
        setting = NotificationSetting.objects.select_for_update().get(pk=setting.pk)
        if not bool(getattr(setting, field, False)):
            return None

        return _refresh_or_create_notification(
            user_id=locked_user.pk,
            recipient_role=Notification.ROLE_AUDIENCE,
            text=text,
            text_en=text_en,
            artist_id=None,
            dedupe_window=dedupe_window,
        )


def send_artist_notification(
    *,
    event: str,
    text: str,
    text_en: str = "",
    artist: Optional[Artist] = None,
    user: Optional[User] = None,
    dedupe_window: timedelta = DEFAULT_DEDUPE_WINDOW,
) -> Optional[Notification]:
    """Create one artist-role notification, isolated from audience preferences.

    Artist notifications can be delivered before an Artist profile exists (for
    example a rejected verification request), so the authenticated user is the
    canonical recipient and ``artist`` is optional context.
    """
    _setting_field(event)  # validate the event name even though settings differ
    explicit_user_id = getattr(user, "pk", None)
    artist_user_id = getattr(artist, "user_id", None)
    if explicit_user_id and artist_user_id and explicit_user_id != artist_user_id:
        logger.error(
            "Blocked artist notification with mismatched owners user_id=%s artist_user_id=%s",
            explicit_user_id,
            artist_user_id,
        )
        return None
    account_id = explicit_user_id or artist_user_id
    if not account_id or not text:
        return None

    with transaction.atomic():
        locked_user = (
            User.objects.select_for_update()
            .only("id", "is_active", "roles")
            .filter(pk=account_id)
            .first()
        )
        if not locked_user or not locked_user.is_active:
            return None

        artist_id = (
            getattr(artist, "pk", None)
            if artist_user_id == locked_user.pk
            else None
        )
        if not artist_id:
            artist_id = (
                Artist.objects.filter(user_id=locked_user.pk)
                .values_list("id", flat=True)
                .first()
            )

        return _refresh_or_create_notification(
            user_id=locked_user.pk,
            recipient_role=Notification.ROLE_ARTIST,
            text=text,
            text_en=text_en,
            artist_id=artist_id,
            dedupe_window=dedupe_window,
        )


def broadcast_user_notification(
    *,
    event: str,
    text: str,
    text_en: str = "",
    exclude_user_ids: Iterable[int] = (),
) -> int:
    """Fan out an audience-role catalog/system notification."""
    field = _setting_field(event)
    excluded = {int(value) for value in exclude_user_ids if value}

    missing_ids = list(
        User.objects.filter(is_active=True, notification_setting__isnull=True)
        .values_list("id", flat=True)
    )
    if missing_ids:
        NotificationSetting.objects.bulk_create(
            [NotificationSetting(user_id=user_id) for user_id in missing_ids],
            ignore_conflicts=True,
        )

    filters = {f"notification_setting__{field}": True, "is_active": True}
    recipients = User.objects.filter(**filters)
    if excluded:
        recipients = recipients.exclude(id__in=excluded)

    created = 0
    batch = []
    for user_id in recipients.values_list("id", flat=True).iterator(chunk_size=2000):
        batch.append(
            Notification(
                user_id=user_id,
                recipient_role=Notification.ROLE_AUDIENCE,
                text=text,
                text_en=text_en or text,
                has_read=False,
            )
        )
        if len(batch) >= 1000:
            created_rows = Notification.objects.bulk_create(batch, batch_size=1000)
            schedule_notification_ids_publish(row.pk for row in created_rows)
            created += len(created_rows)
            batch.clear()

    if batch:
        created_rows = Notification.objects.bulk_create(batch, batch_size=1000)
        schedule_notification_ids_publish(row.pk for row in created_rows)
        created += len(created_rows)
    return created


def send_system_notification(*, user: User, text: str, text_en: str = "") -> Optional[Notification]:
    """Send an audience-role account/payment/system message."""
    return send_user_notification(
        user=user,
        event=EVENT_SYSTEM,
        text=text,
        text_en=text_en,
    )


def send_artist_system_notification(
    *,
    text: str,
    text_en: str = "",
    artist: Optional[Artist] = None,
    user: Optional[User] = None,
) -> Optional[Notification]:
    """Send an artist-dashboard moderation, release, or payout message."""
    return send_artist_notification(
        artist=artist,
        user=user,
        event=EVENT_SYSTEM,
        text=text,
        text_en=text_en,
    )
