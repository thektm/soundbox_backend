"""Exact global trending-song ranking, precomputed outside request workers.

Requests read the shared Redis cache. A dedicated worker refreshes the same
ranking algorithm ahead of expiry; a cold/missing cache still computes exactly
so availability never depends on the worker.
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Max, Q
from django.utils import timezone

from .models import Song
from .performance import CATALOG_VERSION_KEY, cache_get, cache_set, cache_version, stable_cache_key
from .song_play_metrics import get_tracked_song_play_counts

TRENDING_MIN_SONGS = 6
TRENDING_MAX_SONGS = 12
TRENDING_WINDOWS_DAYS = (7, 14, 30, 60, 90, 180, 365)
TRENDING_CACHE_TTL = 180


def _cache_key(require_preview: bool) -> str:
    return stable_cache_key(
        'home-trending-songs',
        'preview' if require_preview else 'full',
        cache_version(CATALOG_VERSION_KEY),
        'v2',
    )


def trending_song_ids(*, require_preview: bool = False, force: bool = False) -> dict:
    """Return the unchanged trending ranking with a cheaper hot-window query.

    The previous implementation aggregated every configured window and all-time
    counts across the whole play relation in one request-time query. This keeps
    the same smallest-useful-window rules, but scans only one window at a time
    and normally stops at seven days. All-time tracked counts used as the final
    tie-breaker come from the exact coherent Redis mirror / PostgreSQL fallback.
    """
    key = _cache_key(require_preview)
    if not force:
        cached = cache_get(key)
        if isinstance(cached, dict) and isinstance(cached.get('ids'), list):
            return cached

    now = timezone.now()
    through = Song.play_counts.through
    base = Q(song__status=Song.STATUS_PUBLISHED)
    if require_preview:
        base &= Q(song__preview_audio_url__isnull=False)
        base &= ~Q(song__preview_audio_url='')

    selected_window = None
    candidates = []
    for days in TRENDING_WINDOWS_DAYS:
        cutoff = now - timedelta(days=days)
        period_rows = list(
            through.objects.filter(base, playcount__created_at__gte=cutoff)
            .values('song_id')
            .annotate(
                recorded_plays=Count('playcount_id'),
                last_play=Max('playcount__created_at'),
            )
        )
        if len(period_rows) >= TRENDING_MIN_SONGS:
            selected_window = days
            candidates = period_rows
            break

    if selected_window is not None:
        candidate_ids = [int(row['song_id']) for row in candidates]
        all_time_tracked = get_tracked_song_play_counts(candidate_ids)
        candidates.sort(
            key=lambda row: (
                int(row.get('recorded_plays') or 0),
                row.get('last_play') or now - timedelta(days=36500),
                int(all_time_tracked.get(int(row['song_id']), 0)),
                int(row['song_id']),
            ),
            reverse=True,
        )
        result = {
            'ids': [int(row['song_id']) for row in candidates[:TRENDING_MAX_SONGS]],
            'window_days': selected_window,
            'is_all_time': False,
        }
    else:
        # Sparse/legacy installations: preserve the old all-time fallback.
        rows = list(
            through.objects.filter(base)
            .values('song_id')
            .annotate(
                recorded_plays_all=Count('playcount_id'),
                last_play_all=Max('playcount__created_at'),
            )
        )
        row_by_song = {int(row['song_id']): row for row in rows}
        song_filter = Q(status=Song.STATUS_PUBLISHED)
        if require_preview:
            song_filter &= Q(preview_audio_url__isnull=False)
            song_filter &= ~Q(preview_audio_url='')
        legacy_rows = Song.objects.filter(song_filter).filter(
            Q(plays__gt=0) | Q(id__in=row_by_song.keys())
        ).values('id', 'plays')

        all_time = []
        for song_row in legacy_rows:
            song_id = int(song_row['id'])
            event_row = row_by_song.get(song_id, {})
            recorded = int(event_row.get('recorded_plays_all') or 0)
            legacy = int(song_row.get('plays') or 0)
            total = recorded + legacy
            if total <= 0:
                continue
            all_time.append({
                'song_id': song_id,
                'score': total,
                'recorded': recorded,
                'last_play': event_row.get('last_play_all'),
            })

        all_time.sort(
            key=lambda row: (
                row['score'],
                row['last_play'] or now - timedelta(days=36500),
                row['recorded'],
                row['song_id'],
            ),
            reverse=True,
        )
        result = {
            'ids': [row['song_id'] for row in all_time[:TRENDING_MAX_SONGS]],
            'window_days': None,
            'is_all_time': True,
        }

    cache_set(key, result, TRENDING_CACHE_TTL)
    return result
