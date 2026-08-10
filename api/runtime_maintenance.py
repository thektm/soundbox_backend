"""Bounded, conservative cleanup for high-churn runtime tables.

Only ephemeral state is deleted here. Plays, payouts, history, user content,
notifications and durable recommendation interactions are intentionally never
part of this cleanup.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone

from .models import ActivePlayback, OtpCode, RefreshToken, StreamAccess
from .recommendation_runtime import cleanup_unused_generated_playlists, get_redis_client

logger = logging.getLogger(__name__)
_LOCK_KEY = "sedabox:runtime-maintenance:lock:v1"


def _delete_in_batches(queryset, *, batch_size=1000, max_rows=None) -> int:
    processed = 0
    max_rows = max(batch_size, int(max_rows or getattr(settings, 'RUNTIME_CLEANUP_MAX_ROWS_PER_TABLE', 5000)))
    while processed < max_rows:
        limit = min(batch_size, max_rows - processed)
        ids = list(queryset.order_by('pk').values_list('pk', flat=True)[:limit])
        if not ids:
            break
        queryset.model.objects.filter(pk__in=ids).delete()
        processed += len(ids)
    return processed


def cleanup_runtime_state(*, startup=False) -> dict[str, int]:
    client = get_redis_client()
    token = f"{timezone.now().timestamp()}"
    if client is not None:
        try:
            if not client.set(_LOCK_KEY, token, nx=True, ex=600):
                return {'skipped': 1}
        except Exception:
            # Cleanup remains safe without Redis because every predicate below is
            # monotonic/ephemeral. Redis only prevents duplicate workers.
            client = None

    now = timezone.now()
    unused_cutoff = now - timedelta(
        hours=max(24, int(getattr(settings, 'STREAM_ACCESS_UNUSED_TTL_HOURS', 168)))
    )
    abandoned_cutoff = now - timedelta(
        days=max(7, int(getattr(settings, 'STREAM_ACCESS_ABANDONED_TTL_DAYS', 14)))
    )
    used_cutoff = now - timedelta(
        days=max(2, int(getattr(settings, 'STREAM_ACCESS_USED_TTL_DAYS', 14)))
    )
    result = {
        'active_playbacks': 0,
        'stream_access_unused': 0,
        'stream_access_abandoned': 0,
        'stream_access_used': 0,
        'expired_otps': 0,
        'expired_refresh_tokens': 0,
    }
    try:
        close_old_connections()
        result['active_playbacks'] = _delete_in_batches(
            ActivePlayback.objects.filter(expiration_time__lt=now), batch_size=2000
        )
        # Never delete a pending ad. Unopened old grants are disposable; once a
        # play has been submitted, the StreamAccess row is only transient proof
        # and the durable PlayCount remains untouched.
        result['stream_access_unused'] = _delete_in_batches(
            StreamAccess.objects.filter(
                created_at__lt=unused_cutoff,
                unwrapped=False,
            ).filter(Q(ad_required=False) | Q(ad_seen=True)),
            batch_size=1000,
        )
        result['stream_access_abandoned'] = _delete_in_batches(
            StreamAccess.objects.filter(
                created_at__lt=abandoned_cutoff,
                unwrapped=True,
                one_time_used=False,
            ).filter(Q(ad_required=False) | Q(ad_seen=True)),
            batch_size=1000,
        )
        result['stream_access_used'] = _delete_in_batches(
            StreamAccess.objects.filter(
                created_at__lt=used_cutoff,
                one_time_used=True,
            ).filter(Q(ad_required=False) | Q(ad_seen=True)),
            batch_size=1000,
        )
        result['expired_otps'] = _delete_in_batches(
            OtpCode.objects.filter(expires_at__lt=now - timedelta(days=1)),
            batch_size=1000,
        )
        result['expired_refresh_tokens'] = _delete_in_batches(
            RefreshToken.objects.filter(expires_at__lt=now - timedelta(days=7)),
            batch_size=1000,
        )
        playlist_result = cleanup_unused_generated_playlists(startup=startup)
        result['generated_playlists'] = int(playlist_result.get('deleted', 0))
        logger.info('Runtime cleanup complete startup=%s result=%s', startup, result)
        return result
    finally:
        close_old_connections()
        if client is not None:
            try:
                if client.get(_LOCK_KEY) == token:
                    client.delete(_LOCK_KEY)
            except Exception:
                pass
