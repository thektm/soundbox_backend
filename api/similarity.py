"""Cached similar-song ranking isolated from serialization concerns."""
from __future__ import annotations

from django.conf import settings
from django.db.models import Q

from .models import Song
from .performance import CATALOG_VERSION_KEY, cache_get_or_claim, cache_set, cache_version, relation_ids, stable_cache_key


def ranked_similar_song_ids(song) -> list[int]:
    genres = relation_ids(song, 'genres')
    moods = relation_ids(song, 'moods')
    tags = relation_ids(song, 'tags')
    key = stable_cache_key('similar-songs-v6', song.pk, song.updated_at, cache_version(CATALOG_VERSION_KEY))
    ranked_ids, claimed = cache_get_or_claim(key, lock_timeout=20, wait_timeout=1.0)
    if ranked_ids is not None:
        return list(ranked_ids)

    match = Q(artist_id=song.artist_id)
    if genres:
        match |= Q(genres__in=genres)
    if moods:
        match |= Q(moods__in=moods)
    if tags:
        match |= Q(tags__in=tags)

    candidates = list(
        Song.objects.filter(status=Song.STATUS_PUBLISHED)
        .exclude(pk=song.pk).filter(match).distinct()
        .select_related('artist', 'album')
        .prefetch_related('genres', 'moods', 'tags')
        .order_by('-plays', '-release_date', '-created_at')[:300]
    )
    source_year = getattr(song.release_date, 'year', None)

    def near(a, b, weight, scale=100):
        return 0 if a is None or b is None else max(0, scale - abs(a - b)) / scale * weight

    scored = []
    for candidate in candidates:
        score = (
            3 * len(genres & relation_ids(candidate, 'genres'))
            + 2 * len(moods & relation_ids(candidate, 'moods'))
            + 1.5 * len(tags & relation_ids(candidate, 'tags'))
        )
        score += 8 if candidate.artist_id == song.artist_id else 0
        year = getattr(candidate.release_date, 'year', None)
        score += 3 if source_year and year and source_year // 10 == year // 10 else 0
        score += (
            near(song.energy, candidate.energy, 3)
            + near(song.danceability, candidate.danceability, 2.5)
            + near(song.valence, candidate.valence, 2)
            + near(song.tempo, candidate.tempo, 1, 200)
        )
        if score > 0:
            scored.append((candidate.pk, score, candidate.plays or 0))

    scored.sort(key=lambda row: (row[1], row[2]), reverse=True)
    ranked_ids = [row[0] for row in scored]
    if not ranked_ids:
        ranked_ids = list(
            Song.objects.filter(status=Song.STATUS_PUBLISHED)
            .exclude(pk=song.pk).order_by('-plays', '-created_at')
            .values_list('id', flat=True)[:100]
        )
    # Only the lock holder should normally compute, but correctness is identical
    # if a timeout caused a second worker to reach here.
    cache_set(key, ranked_ids, getattr(settings, 'CACHE_TTL_SIMILAR', 90))
    return ranked_ids
