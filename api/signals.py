import logging

from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .models import (
    Album,
    AlbumLike,
    Artist,
    ArtistAuth,
    ArtistRelease,
    ArtistMonthlyListener,
    DepositRequest,
    Follow,
    Notification,
    NotificationSetting,
    PlayCount,
    Playlist,
    PaymentTransaction,
    PlaylistLike,
    RecommendedPlaylist,
    Song,
    SongLike,
    User,
    UserImageProfile,
    UserPlaylist,
)
from .notification_service import (
    EVENT_NEW_ALBUM,
    EVENT_NEW_FOLLOWER,
    EVENT_NEW_LIKE,
    EVENT_NEW_PLAYLIST,
    EVENT_NEW_SONG,
    broadcast_user_notification,
    send_artist_notification,
    send_artist_system_notification,
    send_system_notification,
    send_user_notification,
)
from .realtime_notifications import schedule_notification_publish

logger = logging.getLogger(__name__)


def _get_user_display_name(user: User) -> str:
    """Resolve the public actor label used in user-facing notifications.

    A user's chosen first/last name is the primary display identity.  The
    public UID is only the fallback when no name has been set.  This mirrors
    profile/search rendering and avoids follow notifications that expose only
    an opaque identifier even though a readable name is available.
    """
    if not user:
        return "یک کاربر"

    full_name = " ".join(
        part.strip()
        for part in (
            getattr(user, "first_name", "") or "",
            getattr(user, "last_name", "") or "",
        )
        if part and part.strip()
    ).strip()
    if full_name:
        return full_name

    unique_id = (getattr(user, "unique_id", None) or "").strip()
    return unique_id or "یک کاربر"


def _content_label(primary, secondary, numeric_fallback) -> str:
    """Prefer real titles/names; use an id only as the final fallback."""
    for candidate in (primary, secondary):
        value = str(candidate or "").strip()
        if value:
            return value
    return numeric_fallback


def _artist_display_name(artist: Artist | None, *, english: bool = False) -> str:
    """Use the artist's public stage/name fields before falling back to its id."""
    if not artist:
        return "An artist" if english else "یک هنرمند"
    if english:
        return _content_label(
            artist.artistic_name_en or artist.name_en,
            artist.artistic_name or artist.name,
            f"Artist #{artist.pk}",
        )
    return _content_label(
        artist.artistic_name or artist.name,
        artist.artistic_name_en or artist.name_en,
        f"هنرمند شماره {artist.pk}",
    )


def _run_safely(label, callback):
    try:
        callback()
    except Exception:
        # Never break the originating write, but never hide delivery failures.
        logger.exception("Notification job failed: %s", label)


def _after_commit(label, callback):
    transaction.on_commit(lambda: _run_safely(label, callback))


def _linked_user(artist: Artist | None) -> User | None:
    return getattr(artist, "user", None) if artist else None


def _follower_recipient_user_ids(*, artist_ids=(), user_ids=()):
    """Return real user accounts following any supplied artist/user target.

    Both normal-user followers and artist-profile followers are supported; an
    artist follower receives the notification through its linked user account.
    """
    qs = Follow.objects.all()
    if artist_ids and user_ids:
        from django.db.models import Q
        qs = qs.filter(Q(followed_artist_id__in=artist_ids) | Q(followed_user_id__in=user_ids))
    elif artist_ids:
        qs = qs.filter(followed_artist_id__in=artist_ids)
    elif user_ids:
        qs = qs.filter(followed_user_id__in=user_ids)
    else:
        return set()

    recipient_ids = set()
    for follower_user_id, follower_artist_user_id in qs.values_list(
        "follower_user_id", "follower_artist__user_id"
    ):
        recipient_id = follower_user_id or follower_artist_user_id
        if recipient_id:
            recipient_ids.add(recipient_id)
    return recipient_ids


@receiver(post_save, sender=User, dispatch_uid="api.notification.settings.create")
def create_user_notification_settings(sender, instance, created, **kwargs):
    if created:
        NotificationSetting.objects.get_or_create(user=instance)


@receiver(post_save, sender=Notification, dispatch_uid="api.notification.realtime.created")
def publish_created_notification(sender, instance, created, **kwargs):
    """Push committed unread user notifications to connected browser clients."""
    if created and (instance.user_id or instance.artist_id) and not instance.has_read:
        schedule_notification_publish(instance.pk)


@receiver(post_save, sender=Follow, dispatch_uid="api.notification.follow.created")
def notify_new_follower(sender, instance, created, **kwargs):
    if not created:
        return
    follow_id = instance.pk

    def deliver():
        follow = (
            Follow.objects.select_related(
                "follower_user", "follower_artist", "follower_artist__user",
                "followed_user", "followed_artist", "followed_artist__user",
            )
            .filter(pk=follow_id)
            .first()
        )
        if not follow:
            return

        actor_count = int(bool(follow.follower_user_id)) + int(bool(follow.follower_artist_id))
        target_count = int(bool(follow.followed_user_id)) + int(bool(follow.followed_artist_id))
        if actor_count != 1 or target_count != 1:
            logger.warning("Skipping malformed Follow row %s", follow.pk)
            return

        recipient = follow.followed_user or _linked_user(follow.followed_artist)
        actor_user = follow.follower_user or _linked_user(follow.follower_artist)
        if not recipient or (actor_user and actor_user.pk == recipient.pk):
            return

        if follow.follower_user:
            actor_fa = _get_user_display_name(follow.follower_user)
            actor_en = actor_fa
        elif follow.follower_artist:
            actor_fa = _artist_display_name(follow.follower_artist)
            actor_en = _artist_display_name(follow.follower_artist, english=True)
        else:
            actor_fa, actor_en = "یک کاربر", "A user"

        if follow.followed_artist:
            text = f"{actor_fa} صفحه هنرمندی شما را دنبال کرد."
            text_en = f"{actor_en} followed your artist profile."
        else:
            text = f"{actor_fa} شروع به دنبال کردن شما کرد."
            text_en = f"{actor_en} started following you."

        if follow.followed_artist:
            send_artist_notification(
                artist=follow.followed_artist,
                event=EVENT_NEW_FOLLOWER,
                text=text,
                text_en=text_en,
            )
        else:
            send_user_notification(
                user=recipient,
                event=EVENT_NEW_FOLLOWER,
                text=text,
                text_en=text_en,
            )

    _after_commit(f"follow:{follow_id}", deliver)


def _deliver_user_playlist_likes(playlist_id, liker_ids):
    playlist = UserPlaylist.objects.select_related("user").filter(pk=playlist_id).first()
    if not playlist:
        return
    current_liker_ids = set(
        playlist.liked_by.filter(id__in=liker_ids).values_list("id", flat=True)
    )
    for liker in User.objects.filter(id__in=current_liker_ids, is_active=True):
        if liker.pk == playlist.user_id:
            continue
        liker_name = _get_user_display_name(liker)
        playlist_title_fa = _content_label(
            playlist.title, None, f"پلی‌لیست شماره {playlist.pk}"
        )
        send_user_notification(
            user=playlist.user,
            event=EVENT_NEW_LIKE,
            text=f"{liker_name} پلی‌لیست «{playlist_title_fa}» شما را پسندید.",
            text_en=f"{liker_name} liked your playlist '{playlist_title_fa}'.",
        )


@receiver(
    m2m_changed,
    sender=UserPlaylist.liked_by.through,
    dispatch_uid="api.notification.user-playlist.like",
)
def notify_user_playlist_like(sender, instance, action, reverse, pk_set, **kwargs):
    if action != "post_add" or not pk_set:
        return

    if reverse:
        liker_ids = (instance.pk,)
        playlist_ids = tuple(pk_set)
    else:
        liker_ids = tuple(pk_set)
        playlist_ids = (instance.pk,)

    for playlist_id in playlist_ids:
        _after_commit(
            f"user-playlist-like:{playlist_id}",
            lambda playlist_id=playlist_id, liker_ids=liker_ids: _deliver_user_playlist_likes(
                playlist_id, liker_ids
            ),
        )


@receiver(post_save, sender=SongLike, dispatch_uid="api.notification.song.like")
def notify_song_like(sender, instance, created, **kwargs):
    if not created:
        return
    like_id = instance.pk

    def deliver():
        like = (
            SongLike.objects.select_related("user", "song", "song__artist", "song__artist__user")
            .prefetch_related("song__featured_artists__user")
            .filter(pk=like_id)
            .first()
        )
        if not like:
            return
        liker_name = _get_user_display_name(like.user)
        song_title_fa = _content_label(like.song.title, like.song.title_en, f"آهنگ شماره {like.song_id}")
        song_title_en = _content_label(like.song.title_en, like.song.title, f"Song #{like.song_id}")
        contributors = [like.song.artist, *list(like.song.featured_artists.all())]
        delivered_user_ids = set()
        for artist in contributors:
            recipient = _linked_user(artist)
            if (
                not recipient
                or recipient.pk == like.user_id
                or recipient.pk in delivered_user_ids
            ):
                continue
            delivered_user_ids.add(recipient.pk)
            send_artist_notification(
                artist=artist,
                event=EVENT_NEW_LIKE,
                text=f"{liker_name} آهنگ «{song_title_fa}» شما را پسندید.",
                text_en=f"{liker_name} liked your song '{song_title_en}'.",
            )

    _after_commit(f"song-like:{like_id}", deliver)


@receiver(post_save, sender=AlbumLike, dispatch_uid="api.notification.album.like")
def notify_album_like(sender, instance, created, **kwargs):
    if not created:
        return
    like_id = instance.pk

    def deliver():
        like = (
            AlbumLike.objects.select_related("user", "album", "album__artist", "album__artist__user")
            .filter(pk=like_id)
            .first()
        )
        if not like:
            return
        recipient = _linked_user(like.album.artist)
        if not recipient or recipient.pk == like.user_id:
            return
        liker_name = _get_user_display_name(like.user)
        album_title_fa = _content_label(like.album.title, like.album.title_en, f"آلبوم شماره {like.album_id}")
        album_title_en = _content_label(like.album.title_en, like.album.title, f"Album #{like.album_id}")
        send_artist_notification(
            artist=like.album.artist,
            event=EVENT_NEW_LIKE,
            text=f"{liker_name} آلبوم «{album_title_fa}» شما را پسندید.",
            text_en=f"{liker_name} liked your album '{album_title_en}'.",
        )

    _after_commit(f"album-like:{like_id}", deliver)


@receiver(pre_save, sender=Song, dispatch_uid="api.notification.song.capture-status")
def capture_old_song_status(sender, instance, **kwargs):
    if not instance.pk:
        instance._old_status = None
        return
    instance._old_status = (
        Song.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    )


def _deliver_song_release(song_id, followed_artist_ids=None):
    song = (
        Song.objects.select_related("artist", "artist__user")
        .prefetch_related("featured_artists__user")
        .filter(pk=song_id, status=Song.STATUS_PUBLISHED)
        .first()
    )
    if not song:
        return
    # Albums/EPs are announced once through their real album title. Singles and
    # standalone songs continue through the song notification path.
    if song.album_id:
        return

    contributors = [song.artist, *list(song.featured_artists.all())]
    all_artist_ids = {artist.pk for artist in contributors}
    artist_ids = set(followed_artist_ids or all_artist_ids)
    artist_ids &= all_artist_ids
    if not artist_ids:
        return

    excluded = {artist.user_id for artist in contributors if artist.user_id}
    recipient_ids = _follower_recipient_user_ids(artist_ids=artist_ids) - excluded
    recipients = User.objects.filter(id__in=recipient_ids, is_active=True).select_related(
        "notification_setting"
    )
    song_title_fa = _content_label(song.title, song.title_en, f"آهنگ شماره {song.pk}")
    song_title_en = _content_label(song.title_en, song.title, f"Song #{song.pk}")
    artist_name_fa = _artist_display_name(song.artist)
    artist_name_en = _artist_display_name(song.artist, english=True)
    for recipient in recipients.iterator(chunk_size=500):
        send_user_notification(
            user=recipient,
            event=EVENT_NEW_SONG,
            text=f"آهنگ جدید «{song_title_fa}» از {artist_name_fa} منتشر شد!",
            text_en=f"New song '{song_title_en}' by {artist_name_en} is out!",
        )


@receiver(post_save, sender=Song, dispatch_uid="api.notification.song.published")
def notify_new_song_published(sender, instance, created, **kwargs):
    old_status = getattr(instance, "_old_status", None)
    if instance.status != Song.STATUS_PUBLISHED or old_status == Song.STATUS_PUBLISHED:
        return
    song_id = instance.pk
    _after_commit(f"song-published:{song_id}", lambda: _deliver_song_release(song_id))


@receiver(
    m2m_changed,
    sender=Song.featured_artists.through,
    dispatch_uid="api.notification.song.featured-artists",
)
def notify_published_song_featured_artist_followers(sender, instance, action, reverse, pk_set, **kwargs):
    if action != "post_add" or not pk_set:
        return

    if reverse:
        artist_id = instance.pk
        for song_id in tuple(pk_set):
            _after_commit(
                f"song-featured:{song_id}:{artist_id}",
                lambda song_id=song_id, artist_id=artist_id: _deliver_song_release(
                    song_id, {artist_id}
                ),
            )
    elif instance.status == Song.STATUS_PUBLISHED:
        song_id = instance.pk
        artist_ids = set(pk_set)
        _after_commit(
            f"song-featured:{song_id}",
            lambda: _deliver_song_release(song_id, artist_ids),
        )


@receiver(post_save, sender=Album, dispatch_uid="api.notification.album.created")
def notify_new_album_created(sender, instance, created, **kwargs):
    if not created:
        return
    album_id = instance.pk

    def deliver():
        album = (
            Album.objects.select_related("artist", "artist__user")
            .filter(pk=album_id)
            .first()
        )
        if not album or not album.songs.filter(status=Song.STATUS_PUBLISHED).exists():
            return
        excluded = {album.artist.user_id} if album.artist.user_id else set()
        recipient_ids = _follower_recipient_user_ids(artist_ids={album.artist_id}) - excluded
        recipients = User.objects.filter(id__in=recipient_ids, is_active=True).select_related(
            "notification_setting"
        )
        album_title_fa = _content_label(album.title, album.title_en, f"آلبوم شماره {album.pk}")
        album_title_en = _content_label(album.title_en, album.title, f"Album #{album.pk}")
        artist_name_fa = _artist_display_name(album.artist)
        artist_name_en = _artist_display_name(album.artist, english=True)
        for recipient in recipients.iterator(chunk_size=500):
            send_user_notification(
                user=recipient,
                event=EVENT_NEW_ALBUM,
                text=f"آلبوم جدید «{album_title_fa}» از {artist_name_fa} منتشر شد!",
                text_en=f"New album '{album_title_en}' by {artist_name_en} is out!",
            )

    _after_commit(f"album-created:{album_id}", deliver)


@receiver(pre_save, sender=UserPlaylist, dispatch_uid="api.notification.user-playlist.capture-public")
def capture_old_user_playlist_public(sender, instance, **kwargs):
    if not instance.pk:
        instance._old_public = False
        return
    instance._old_public = bool(
        UserPlaylist.objects.filter(pk=instance.pk).values_list("public", flat=True).first()
    )


@receiver(post_save, sender=UserPlaylist, dispatch_uid="api.notification.user-playlist.public")
def notify_public_user_playlist(sender, instance, created, **kwargs):
    became_public = instance.public and (created or not getattr(instance, "_old_public", False))
    if not became_public:
        return
    playlist_id = instance.pk

    def deliver():
        playlist = UserPlaylist.objects.select_related("user").filter(pk=playlist_id, public=True).first()
        if not playlist:
            return
        recipient_ids = _follower_recipient_user_ids(user_ids={playlist.user_id}) - {playlist.user_id}
        owner_name = _get_user_display_name(playlist.user)
        playlist_title = _content_label(
            playlist.title, None, f"پلی‌لیست شماره {playlist.pk}"
        )
        for recipient in User.objects.filter(id__in=recipient_ids, is_active=True).select_related(
            "notification_setting"
        ).iterator(chunk_size=500):
            send_user_notification(
                user=recipient,
                event=EVENT_NEW_PLAYLIST,
                text=f"{owner_name} پلی‌لیست عمومی جدید «{playlist_title}» را منتشر کرد.",
                text_en=f"{owner_name} published a new public playlist, '{playlist_title}'.",
            )

    _after_commit(f"user-playlist-public:{playlist_id}", deliver)


@receiver(post_save, sender=Playlist, dispatch_uid="api.notification.catalog-playlist.created")
def notify_new_catalog_playlist(sender, instance, created, **kwargs):
    if not created or instance.created_by not in {
        Playlist.CREATED_BY_ADMIN,
        Playlist.CREATED_BY_SYSTEM,
    }:
        return
    playlist_id = instance.pk

    def deliver():
        playlist = Playlist.objects.filter(
            pk=playlist_id,
            created_by__in={Playlist.CREATED_BY_ADMIN, Playlist.CREATED_BY_SYSTEM},
        ).first()
        if not playlist:
            return
        playlist_title_fa = _content_label(
            playlist.title, playlist.title_en, f"پلی‌لیست شماره {playlist.pk}"
        )
        playlist_title_en = _content_label(
            playlist.title_en, playlist.title, f"Playlist #{playlist.pk}"
        )
        broadcast_user_notification(
            event=EVENT_NEW_PLAYLIST,
            text=f"پلی‌لیست جدید «{playlist_title_fa}» به صداباکس اضافه شد.",
            text_en=f"New playlist '{playlist_title_en}' was added to Sedabox.",
        )

    _after_commit(f"catalog-playlist-created:{playlist_id}", deliver)



def _capture_previous_status(instance, model):
    if not instance.pk:
        instance._notification_old_status = None
        return
    instance._notification_old_status = (
        model.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    )


def _status_changed(instance) -> bool:
    return getattr(instance, "_notification_old_status", None) != instance.status


@receiver(pre_save, sender=ArtistRelease, dispatch_uid="api.notification.artist-release.capture-status")
def capture_old_artist_release_status(sender, instance, **kwargs):
    _capture_previous_status(instance, ArtistRelease)


@receiver(post_save, sender=ArtistRelease, dispatch_uid="api.notification.artist-release.status")
def notify_artist_release_status(sender, instance, created, **kwargs):
    visible_statuses = {
        ArtistRelease.STATUS_CHANGES_REQUESTED,
        ArtistRelease.STATUS_APPROVED,
        ArtistRelease.STATUS_SCHEDULED,
        ArtistRelease.STATUS_LIVE,
        ArtistRelease.STATUS_REJECTED,
        ArtistRelease.STATUS_TAKEN_DOWN,
    }
    if created or not _status_changed(instance) or instance.status not in visible_statuses:
        return

    release_id = instance.pk
    expected_status = instance.status

    def deliver():
        release = ArtistRelease.objects.select_related("artist", "artist__user").filter(
            pk=release_id,
            status=expected_status,
        ).first()
        if not release or not release.artist.user_id:
            return

        title_fa = _content_label(release.title, release.title_en, f"انتشار شماره {release.pk}")
        title_en = _content_label(release.title_en, release.title, f"Release #{release.pk}")
        if expected_status == ArtistRelease.STATUS_CHANGES_REQUESTED:
            text = f"برای انتشار «{title_fa}» درخواست اصلاح ثبت شد."
            text_en = f"Changes were requested for your release '{title_en}'."
        elif expected_status == ArtistRelease.STATUS_APPROVED:
            text = f"انتشار «{title_fa}» تأیید شد."
            text_en = f"Your release '{title_en}' was approved."
        elif expected_status == ArtistRelease.STATUS_SCHEDULED:
            scheduled = timezone.localtime(release.scheduled_at).strftime('%Y-%m-%d %H:%M') if release.scheduled_at else "زمان تعیین‌شده"
            text = f"انتشار «{title_fa}» برای {scheduled} زمان‌بندی شد."
            text_en = f"Your release '{title_en}' was scheduled for {scheduled}."
        elif expected_status == ArtistRelease.STATUS_LIVE:
            text = f"انتشار «{title_fa}» منتشر شد و اکنون در دسترس مخاطبان است."
            text_en = f"Your release '{title_en}' is now live."
        elif expected_status == ArtistRelease.STATUS_REJECTED:
            text = f"انتشار «{title_fa}» تأیید نشد. برای جزئیات، یادداشت بررسی را مشاهده کنید."
            text_en = f"Your release '{title_en}' was rejected. Review the moderation note for details."
        else:
            text = f"انتشار «{title_fa}» از دسترس مخاطبان خارج شد."
            text_en = f"Your release '{title_en}' was taken down."

        send_artist_system_notification(
            artist=release.artist,
            text=text,
            text_en=text_en,
        )

    _after_commit(f"artist-release-status:{release_id}:{expected_status}", deliver)


@receiver(post_save, sender=ArtistAuth, dispatch_uid="api.artist-auth.provision-profile")
def provision_approved_artist_auth(sender, instance, **kwargs):
    if instance.status != ArtistAuth.STATUS_ACCEPTED or not instance.is_verified:
        return
    auth_id = instance.pk

    def provision():
        from .artist_auth_service import provision_artist_profile
        provision_artist_profile(auth_id)

    _after_commit(f"artist-auth-profile:{auth_id}", provision)


@receiver(pre_save, sender=ArtistAuth, dispatch_uid="api.notification.artist-auth.capture-status")
def capture_old_artist_auth_status(sender, instance, **kwargs):
    _capture_previous_status(instance, ArtistAuth)


@receiver(post_save, sender=ArtistAuth, dispatch_uid="api.notification.artist-auth.status")
def notify_artist_auth_status(sender, instance, created, **kwargs):
    if not _status_changed(instance) or instance.status not in {
        ArtistAuth.STATUS_ACCEPTED,
        ArtistAuth.STATUS_REJECTED,
    }:
        return
    auth_id = instance.pk
    expected_status = instance.status

    def deliver():
        auth = ArtistAuth.objects.select_related("user").filter(
            pk=auth_id,
            status=expected_status,
        ).first()
        if not auth or not auth.user:
            return
        if expected_status == ArtistAuth.STATUS_ACCEPTED:
            text = "درخواست احراز هویت هنرمندی شما تأیید شد."
            text_en = "Your artist verification request was approved."
        else:
            text = "درخواست احراز هویت هنرمندی شما رد شد. برای جزئیات با پشتیبانی تماس بگیرید."
            text_en = "Your artist verification request was rejected. Contact support for details."
        send_artist_system_notification(user=auth.user, text=text, text_en=text_en)

    _after_commit(f"artist-auth-status:{auth_id}:{expected_status}", deliver)


@receiver(pre_save, sender=UserImageProfile, dispatch_uid="api.notification.profile-image.capture-status")
def capture_old_profile_image_status(sender, instance, **kwargs):
    _capture_previous_status(instance, UserImageProfile)


@receiver(post_save, sender=UserImageProfile, dispatch_uid="api.notification.profile-image.status")
def notify_profile_image_status(sender, instance, created, **kwargs):
    if not _status_changed(instance) or instance.status not in {
        UserImageProfile.STATUS_PUBLISHED,
        UserImageProfile.STATUS_REJECTED,
    }:
        return
    profile_id = instance.pk
    expected_status = instance.status

    def deliver():
        profile = UserImageProfile.objects.select_related("user").filter(
            pk=profile_id,
            status=expected_status,
        ).first()
        if not profile:
            return
        if expected_status == UserImageProfile.STATUS_PUBLISHED:
            text = "تصویر پروفایل شما تأیید و منتشر شد."
            text_en = "Your profile image was approved and published."
        else:
            text = "تصویر پروفایل شما تأیید نشد. لطفاً تصویر دیگری بارگذاری کنید."
            text_en = "Your profile image was not approved. Please upload a different image."
        send_system_notification(user=profile.user, text=text, text_en=text_en)

    _after_commit(f"profile-image-status:{profile_id}:{expected_status}", deliver)


@receiver(pre_save, sender=PaymentTransaction, dispatch_uid="api.notification.payment.capture-status")
def capture_old_payment_status(sender, instance, **kwargs):
    _capture_previous_status(instance, PaymentTransaction)


@receiver(post_save, sender=PaymentTransaction, dispatch_uid="api.notification.payment.status")
def notify_payment_status(sender, instance, created, **kwargs):
    if not _status_changed(instance) or instance.status not in {
        PaymentTransaction.STATUS_SUCCESS,
        PaymentTransaction.STATUS_FAILED,
    }:
        return
    transaction_pk = instance.pk
    expected_status = instance.status

    def deliver():
        payment = PaymentTransaction.objects.select_related("user").filter(
            pk=transaction_pk,
            status=expected_status,
        ).first()
        if not payment:
            return
        reference = payment.transaction_id
        if expected_status == PaymentTransaction.STATUS_SUCCESS:
            text = f"پرداخت شما با شناسه «{reference}» با موفقیت انجام شد."
            text_en = f"Your payment with reference '{reference}' was successful."
        else:
            text = f"پرداخت شما با شناسه «{reference}» ناموفق بود."
            text_en = f"Your payment with reference '{reference}' failed."
        send_system_notification(user=payment.user, text=text, text_en=text_en)

    _after_commit(f"payment-status:{transaction_pk}:{expected_status}", deliver)


@receiver(pre_save, sender=DepositRequest, dispatch_uid="api.notification.deposit.capture-status")
def capture_old_deposit_status(sender, instance, **kwargs):
    _capture_previous_status(instance, DepositRequest)


@receiver(post_save, sender=DepositRequest, dispatch_uid="api.notification.deposit.status")
def notify_deposit_status(sender, instance, created, **kwargs):
    if not _status_changed(instance) or instance.status not in {
        DepositRequest.STATUS_APPROVED,
        DepositRequest.STATUS_REJECTED,
        DepositRequest.STATUS_DONE,
    }:
        return
    deposit_id = instance.pk
    expected_status = instance.status

    def deliver():
        deposit = DepositRequest.objects.select_related("artist", "artist__user").filter(
            pk=deposit_id,
            status=expected_status,
        ).first()
        if not deposit or not deposit.artist.user:
            return
        if expected_status == DepositRequest.STATUS_APPROVED:
            text = "درخواست تسویه شما تأیید شد و در صف پرداخت قرار گرفت."
            text_en = "Your payout request was approved and queued for payment."
        elif expected_status == DepositRequest.STATUS_DONE:
            text = "تسویه شما با موفقیت انجام شد."
            text_en = "Your payout was completed successfully."
        else:
            text = "درخواست تسویه شما رد شد. برای جزئیات با پشتیبانی تماس بگیرید."
            text_en = "Your payout request was rejected. Contact support for details."
        send_artist_system_notification(artist=deposit.artist, text=text, text_en=text_en)

    _after_commit(f"deposit-status:{deposit_id}:{expected_status}", deliver)


def _deliver_song_owner_status(song_id, expected_status):
    song = Song.objects.select_related("artist", "artist__user").filter(
        pk=song_id,
        status=expected_status,
    ).first()
    if not song or not song.artist.user:
        return
    # Release-linked tracks are represented by one release-level notification;
    # sending one card per track creates noisy duplicates for EPs/albums.
    if song.release_track_links.exists():
        return

    title_fa = _content_label(song.title, song.title_en, f"آهنگ شماره {song.pk}")
    title_en = _content_label(song.title_en, song.title, f"Song #{song.pk}")
    if expected_status == Song.STATUS_APPROVED:
        text = f"آهنگ «{title_fa}» تأیید شد."
        text_en = f"Your song '{title_en}' was approved."
    elif expected_status == Song.STATUS_PUBLISHED:
        text = f"آهنگ «{title_fa}» منتشر شد."
        text_en = f"Your song '{title_en}' was published."
    else:
        text = f"آهنگ «{title_fa}» تأیید نشد. برای جزئیات با پشتیبانی تماس بگیرید."
        text_en = f"Your song '{title_en}' was rejected. Contact support for details."
    send_artist_system_notification(artist=song.artist, text=text, text_en=text_en)


@receiver(post_save, sender=Song, dispatch_uid="api.notification.song.owner-status")
def notify_song_owner_status(sender, instance, created, **kwargs):
    old_status = getattr(instance, "_old_status", None)
    if old_status == instance.status or instance.status not in {
        Song.STATUS_APPROVED,
        Song.STATUS_REJECTED,
        Song.STATUS_PUBLISHED,
    }:
        return
    song_id = instance.pk
    expected_status = instance.status
    _after_commit(
        f"song-owner-status:{song_id}:{expected_status}",
        lambda: _deliver_song_owner_status(song_id, expected_status),
    )


# Keep cached similarity rankings fresh without caching response payloads.
from .performance import (
    AFFINITY_VERSION_KEY,
    CATALOG_VERSION_KEY,
    USER_DIRECTORY_VERSION_KEY,
    cache_increment,
    bump_user_affinity_version,
)

_VERSION_TTL = 7 * 24 * 60 * 60


def _bump_catalog(**_kwargs):
    cache_increment(CATALOG_VERSION_KEY, _VERSION_TTL)


def _bump_affinity(**_kwargs):
    cache_increment(AFFINITY_VERSION_KEY, _VERSION_TTL)


def _bump_affinity_on_create(created=False, **_kwargs):
    if created:
        _bump_affinity()


def _connect_m2m_version_bump(model, field_names, prefix):
    for field_name in field_names:
        try:
            field = model._meta.get_field(field_name)
        except Exception:
            continue
        if not getattr(field, 'many_to_many', False):
            continue
        m2m_changed.connect(
            _bump_catalog,
            sender=field.remote_field.through,
            dispatch_uid=f"{prefix}.{field_name}",
        )


post_save.connect(_bump_catalog, sender=Song, dispatch_uid="api.song.catalog.save")
post_delete.connect(_bump_catalog, sender=Song, dispatch_uid="api.song.catalog.delete")
_connect_m2m_version_bump(Song, ("genres", "moods", "tags"), "api.song.catalog")

post_save.connect(_bump_affinity, sender=Follow, dispatch_uid="api.affinity.follow.save")
post_delete.connect(_bump_affinity, sender=Follow, dispatch_uid="api.affinity.follow.delete")
post_save.connect(
    _bump_affinity_on_create,
    sender=ArtistMonthlyListener,
    dispatch_uid="api.affinity.listener.create",
)
post_delete.connect(
    _bump_affinity,
    sender=ArtistMonthlyListener,
    dispatch_uid="api.affinity.listener.delete",
)


def _bump_user_directory(**_kwargs):
    cache_increment(USER_DIRECTORY_VERSION_KEY, _VERSION_TTL)


for model in (Artist, Album, Playlist, RecommendedPlaylist):
    post_save.connect(_bump_catalog, sender=model, dispatch_uid=f"api.catalog.{model.__name__}.save")
    post_delete.connect(_bump_catalog, sender=model, dispatch_uid=f"api.catalog.{model.__name__}.delete")

for model, fields in (
    (Album, ('genres', 'sub_genres', 'moods')),
    (Playlist, ('songs', 'genres', 'moods', 'tags')),
    (RecommendedPlaylist, ('songs',)),
):
    _connect_m2m_version_bump(model, fields, f"api.catalog.{model.__name__}")

post_save.connect(_bump_user_directory, sender=User, dispatch_uid='api.user.directory.save')
post_delete.connect(_bump_user_directory, sender=User, dispatch_uid='api.user.directory.delete')


# Keep per-user recommendation pools current without invalidating every user's
# cache whenever one account interacts with music.
def _bump_instance_user_affinity(sender=None, instance=None, **_kwargs):
    bump_user_affinity_version(getattr(instance, 'user_id', None))


def _bump_follow_user_affinity(sender=None, instance=None, **_kwargs):
    bump_user_affinity_version(getattr(instance, 'follower_user_id', None))


def _bump_user_playlist_songs(sender=None, instance=None, action=None, **_kwargs):
    if action in {'post_add', 'post_remove', 'post_clear'}:
        bump_user_affinity_version(getattr(instance, 'user_id', None))


def _bump_recommended_interactions(sender=None, action=None, pk_set=None, **_kwargs):
    if action in {'post_add', 'post_remove', 'post_clear'}:
        for user_id in pk_set or ():
            bump_user_affinity_version(user_id)


for interaction_model in (SongLike, AlbumLike, PlaylistLike, PlayCount):
    post_save.connect(
        _bump_instance_user_affinity,
        sender=interaction_model,
        dispatch_uid=f'api.user-affinity.{interaction_model.__name__}.save',
    )
    post_delete.connect(
        _bump_instance_user_affinity,
        sender=interaction_model,
        dispatch_uid=f'api.user-affinity.{interaction_model.__name__}.delete',
    )

post_save.connect(
    _bump_follow_user_affinity, sender=Follow,
    dispatch_uid='api.user-affinity.follow.save',
)
post_delete.connect(
    _bump_follow_user_affinity, sender=Follow,
    dispatch_uid='api.user-affinity.follow.delete',
)
m2m_changed.connect(
    _bump_user_playlist_songs, sender=UserPlaylist.songs.through,
    dispatch_uid='api.user-affinity.user-playlist.songs',
)
for field_name in ('liked_by', 'saved_by', 'viewed_by'):
    m2m_changed.connect(
        _bump_recommended_interactions,
        sender=RecommendedPlaylist._meta.get_field(field_name).remote_field.through,
        dispatch_uid=f'api.user-affinity.recommended.{field_name}',
    )
