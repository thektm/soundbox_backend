"""Central notification delivery and preference enforcement.

Every application notification must pass through this module. Keeping the
preference mapping in one place prevents signals, views, admin actions, and
future jobs from silently bypassing the user's notification settings.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Iterable, Optional

from django.db import transaction
from django.utils import timezone

from .models import Notification, NotificationSetting, User
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

# Separate signal paths can represent the same logical event (for example a
# listener following both a song's primary and featured artist). A short window
# suppresses only those near-simultaneous duplicates; later real activity is
# still delivered normally.
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
    """Return the recipient's current persisted preference for ``event``.

    The value is read from the database instead of a possibly stale reverse
    relation cached on a long-lived ``User`` instance. This makes a toggle take
    effect immediately for all producers.
    """
    if not user or not getattr(user, "pk", None):
        return False

    field = _setting_field(event)
    active = User.objects.filter(pk=user.pk, is_active=True).exists()
    if not active:
        return False

    setting = _get_or_create_setting(user.pk)
    return bool(getattr(setting, field, False))


def send_user_notification(
    *,
    user: Optional[User],
    event: str,
    text: str,
    text_en: str = "",
    dedupe_window: timedelta = DEFAULT_DEDUPE_WINDOW,
) -> Optional[Notification]:
    """Create one preference-aware notification for a user.

    Preference evaluation and notification insertion happen in one transaction.
    The recipient and preference rows are locked so a concurrent toggle has a
    deterministic order relative to delivery. Near-simultaneous duplicate
    signals refresh one unread row rather than creating duplicate cards.
    """
    if not user or not getattr(user, "pk", None) or not text:
        return None

    field = _setting_field(event)
    now = timezone.now()
    cutoff = now - dedupe_window

    with transaction.atomic():
        # The user lock serializes concurrent notification producers for the
        # same recipient, including the empty-queryset insert case.
        locked_user = (
            User.objects.select_for_update()
            .only("id", "is_active")
            .filter(pk=user.pk)
            .first()
        )
        if not locked_user or not locked_user.is_active:
            return None

        # Lock the preference row as well. A concurrent PATCH of the same row
        # will therefore be ordered either before or after this delivery.
        setting, _ = NotificationSetting.objects.get_or_create(user_id=locked_user.pk)
        setting = NotificationSetting.objects.select_for_update().get(pk=setting.pk)
        if not bool(getattr(setting, field, False)):
            return None

        existing = (
            Notification.objects.select_for_update()
            .filter(user_id=locked_user.pk, text=text, created_at__gte=cutoff)
            .order_by("-created_at", "-id")
            .first()
        )
        if existing:
            # A still-unread match is the same logical event arriving through a
            # duplicate signal path, so do not send a second toast. If the row
            # was already read, reopening it is a new visible event and must be
            # pushed after commit.
            should_publish = bool(existing.has_read)
            Notification.objects.filter(pk=existing.pk).update(
                has_read=False,
                created_at=now,
                text_en=text_en or existing.text_en or text,
            )
            existing.has_read = False
            existing.created_at = now
            existing.text_en = text_en or existing.text_en or text
            if should_publish:
                schedule_notification_publish(existing.pk)
            return existing

        # Creation is published by the Notification post-save receiver after
        # the outermost transaction commits.
        return Notification.objects.create(
            user_id=locked_user.pk,
            text=text,
            text_en=text_en or text,
            has_read=False,
        )


def broadcast_user_notification(
    *,
    event: str,
    text: str,
    text_en: str = "",
    exclude_user_ids: Iterable[int] = (),
) -> int:
    """Fan out a rare catalog/system announcement to opted-in active users.

    Existing settings are used as the authoritative filter. Legacy users are
    backfilled first, then inserts are streamed in bounded batches.
    """
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
    """Send an account, moderation, payment, or application system message."""
    return send_user_notification(
        user=user,
        event=EVENT_SYSTEM,
        text=text,
        text_en=text_en,
    )
