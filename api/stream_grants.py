"""Stateless stream grants used by read serializers.

A song card only needs to grant permission to *start* a stream. Persisting a
``StreamAccess`` row while serializing a card turns every read/search/home
response into write-heavy traffic. These signed grants keep the public URL
contract unchanged and materialize the existing StreamAccess accounting row
only when the URL is actually opened.

Existing database-backed short tokens remain valid; the stream view checks
those first for full backwards compatibility.
"""
from __future__ import annotations

import hashlib
import secrets

from django.conf import settings
from django.core import signing
from django.db import IntegrityError, transaction
from django.urls import reverse

from .models import Song, StreamAccess
from .utils import absolute_api_url

_SALT = "sedabox.stream-grant.v1"


def create_stream_grant_token(user_id: int, song_id: int) -> str:
    return signing.dumps(
        {"u": int(user_id), "s": int(song_id), "n": secrets.token_urlsafe(6)},
        salt=_SALT,
        compress=True,
    )


def create_stream_grant_url(request, song) -> str | None:
    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False) or not getattr(song, "pk", None):
        return None
    token = create_stream_grant_token(user.pk, song.pk)
    return absolute_api_url(request, reverse("stream-short", kwargs={"token": token}))


def stream_grant_identity(token: str) -> tuple[int, int] | None:
    """Return the signed ``(user_id, song_id)`` pair without authorizing it."""
    try:
        payload = signing.loads(
            token,
            salt=_SALT,
            max_age=max(60, int(getattr(settings, 'STREAM_GRANT_MAX_AGE_SECONDS', 7 * 24 * 60 * 60))),
        )
        user_id = int(payload.get("u", 0))
        song_id = int(payload.get("s", 0))
        if user_id <= 0 or song_id <= 0:
            return None
        return user_id, song_id
    except (signing.BadSignature, TypeError, ValueError, AttributeError):
        return None


def decode_stream_grant(token: str, *, user_id: int) -> int | None:
    identity = stream_grant_identity(token)
    if not identity or identity[0] != int(user_id):
        return None
    return identity[1]


def materialize_stream_grant(token: str, *, user) -> StreamAccess | None:
    """Create/reuse one accounting row when a signed stream URL is consumed.

    ``unwrap_token`` stores only a SHA-256 fingerprint, never the signed grant
    itself. Its unique index provides race-safe idempotence when a client
    retries the same URL concurrently.
    """
    song_id = decode_stream_grant(token, user_id=user.pk)
    if not song_id:
        return None

    fingerprint = hashlib.sha256(token.encode("utf-8", "ignore")).hexdigest()
    existing = StreamAccess.objects.select_related("song", "user").filter(
        unwrap_token=fingerprint,
        user=user,
    ).first()
    if existing:
        return existing

    # Match the old behavior: the song was authorized when the card was
    # serialized, and a later status transition does not mutate that URL.
    song = Song.objects.filter(pk=song_id).first()
    if song is None:
        return None

    for _ in range(3):
        try:
            with transaction.atomic():
                return StreamAccess.objects.create(
                    user=user,
                    song=song,
                    unwrap_token=fingerprint,
                    short_token=secrets.token_urlsafe(9)[:12],
                    unique_otplay_id=secrets.token_urlsafe(18),
                )
        except IntegrityError:
            existing = StreamAccess.objects.select_related("song", "user").filter(
                unwrap_token=fingerprint,
                user=user,
            ).first()
            if existing:
                return existing
    return None
