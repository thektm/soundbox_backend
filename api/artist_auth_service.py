"""Provision and keep an approved artist-auth submission linked to one profile."""

from __future__ import annotations

import logging
import os

from django.db import transaction

from .models import Artist, ArtistAuth, User
from .utils import upload_file_to_r2

logger = logging.getLogger(__name__)


def _full_name(first: str, last: str, fallback: str) -> str:
    value = " ".join(part.strip() for part in (first or "", last or "") if part and part.strip())
    return value or (fallback or "Artist").strip() or "Artist"


def _unique_name(value: str, auth_id: int, *, exclude_id: int | None = None) -> str:
    value = (value or "Artist").strip()[:255] or "Artist"
    qs = Artist.objects.filter(name=value)
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    if not qs.exists():
        return value
    suffix = f" #{auth_id}"
    return f"{value[: 255 - len(suffix)]}{suffix}"


def _profile_image_url(auth: ArtistAuth, user: User) -> str:
    if not auth.profile_image:
        return ""
    try:
        extension = os.path.splitext(auth.profile_image.name or "profile.jpg")[1] or ".jpg"
        filename = f"artist-{user.unique_id or user.pk}-profile{extension.lower()}"
        auth.profile_image.open("rb")
        url, _ = upload_file_to_r2(
            auth.profile_image,
            folder="artists/profiles",
            custom_filename=filename,
            check_existing=False,
        )
        return url
    except Exception:
        logger.exception("Could not upload approved artist-auth profile image: auth=%s", auth.pk)
        return ""


def provision_artist_profile(auth_id: int) -> Artist | None:
    """Create/link one artist profile for an approved auth; safe to call repeatedly."""
    with transaction.atomic():
        # Lock only the auth row; nullable select_related joins cannot be used
        # with FOR UPDATE on PostgreSQL. Related rows are locked separately.
        auth = ArtistAuth.objects.select_for_update().filter(pk=auth_id).first()
        if not auth or not auth.user or auth.status != ArtistAuth.STATUS_ACCEPTED or not auth.is_verified:
            return None

        user = User.objects.select_for_update().get(pk=auth.user_id)
        linked = Artist.objects.select_for_update().filter(user=user).first()

        if auth.auth_type == ArtistAuth.AUTH_EXISTING:
            profile = (
                Artist.objects.select_for_update()
                .filter(pk=auth.artist_claimed_id)
                .first()
            )
            if not profile:
                raise ValueError("Approved existing-artist authentication has no claimed artist.")
            if profile.user_id not in (None, user.pk):
                raise ValueError("The claimed artist is already linked to another user.")
            if linked and linked.pk != profile.pk:
                raise ValueError("This user is already linked to another artist profile.")
        else:
            profile = linked

        fa_name = _full_name(auth.first_name, auth.last_name, auth.stage_name)
        en_name = _full_name(
            auth.first_name_en, auth.last_name_en, auth.stage_name_en or fa_name
        )
        image_url = _profile_image_url(auth, user)

        if profile is None:
            profile = Artist(
                user=user,
                name=_unique_name(fa_name, auth.pk),
                unique_id=(
                    user.unique_id
                    if user.unique_id and not Artist.objects.filter(unique_id=user.unique_id).exists()
                    else None
                ),
            )

        # Existing claimed profiles keep established public names; fresh profiles mirror the submission.
        if auth.auth_type == ArtistAuth.AUTH_FRESH:
            profile.name = _unique_name(fa_name, auth.pk, exclude_id=profile.pk)
            profile.name_en = en_name
            profile.artistic_name = auth.stage_name.strip()
            profile.artistic_name_en = auth.stage_name_en.strip() or auth.stage_name.strip()
        else:
            profile.name_en = profile.name_en or en_name
            profile.artistic_name = profile.artistic_name or auth.stage_name.strip()
            profile.artistic_name_en = profile.artistic_name_en or auth.stage_name_en.strip()

        profile.user = user
        profile.email = auth.email or user.email
        profile.city = auth.city
        profile.city_en = auth.city
        profile.date_of_birth = auth.birth_date
        profile.address = auth.address or ""
        profile.address_en = auth.address or ""
        profile.id_number = auth.national_id
        profile.bio = auth.biography or ""
        profile.bio_en = auth.biography_en or auth.biography or ""
        profile.verified = True
        if image_url:
            profile.profile_image = image_url
        profile.save()

        roles = list(user.roles or [])
        if User.ROLE_ARTIST not in roles:
            roles.append(User.ROLE_ARTIST)
        user.roles = roles
        user.save(update_fields=["roles"])

        if auth.artist_claimed_id != profile.pk:
            ArtistAuth.objects.filter(pk=auth.pk).update(artist_claimed=profile)

        return profile
