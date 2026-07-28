from django.db.models.signals import post_save, post_delete, m2m_changed, pre_save
from django.dispatch import receiver
from .models import User, Song, Album, Artist, Playlist, RecommendedPlaylist, Follow, UserPlaylist, Notification, NotificationSetting, ArtistMonthlyListener
from django.utils.translation import gettext as _
from django.utils import timezone

def _get_user_display_name(u: User):
    """Helper to resolve a precise and friendly display name for a user."""
    if not u:
        return "یک کاربر"
    
    # 1. Prefer unique_id (ensure it's not just whitespace or None)
    uid = getattr(u, 'unique_id', None)
    if uid and str(uid).strip():
        return str(uid).strip()

    # 2. Prefer first_name + last_name
    first = getattr(u, 'first_name', '') or ''
    last = getattr(u, 'last_name', '') or ''
    names = f"{first} {last}".strip()
    if names:
        return names
    
    # 3. Fallback to a generic string (could also use partially hidden phone if preferred)
    return "یک کاربر"

def _send_or_update_notification(user_or_artist, text, text_en=None):
    """
    Creates a new notification or updates an existing one if the text is identical.
    Ensures 'created_at' is updated to now and 'has_read' is reset to False.
    """
    now = timezone.now()
    if isinstance(user_or_artist, User):
        lookup = {'user': user_or_artist, 'text': text}
    else: # Artist
        lookup = {'artist': user_or_artist, 'text': text}
    
    # Try to find an existing notification to avoid duplicates from follow/unfollow cycles
    existing = Notification.objects.filter(**lookup).first()
    if existing:
        # Update existing record and move to top
        existing.has_read = False
        existing.created_at = now
        # We need to use update() to bypass auto_now_add if we want to force time change efficiently
        # OR just call save() which usually doesn't update auto_now_add.
        # However, for the user's "time is wrong" fix, we will manually update via queryset.
        Notification.objects.filter(pk=existing.pk).update(has_read=False, created_at=now, text_en=text_en or existing.text_en)
    else:
        Notification.objects.create(**lookup, text_en=text_en or text)

@receiver(post_save, sender=User)
def create_user_notification_settings(sender, instance, created, **kwargs):
    """Automatically create notification settings for new users."""
    if created:
        NotificationSetting.objects.get_or_create(user=instance)

@receiver(post_save, sender=Follow)
def notify_new_follower(sender, instance, created, **kwargs):
    """Notify a user when someone starts following them."""
    if created and instance.followed_user:
        target_user = instance.followed_user
        try:
            # Refresh from DB to ensure setting exists
            setting, _ = NotificationSetting.objects.get_or_create(user=target_user)
            if setting.new_follower:
                # Resolve a friendly display name for the follower carefully:
                if instance.follower_user:
                    follower_name = _get_user_display_name(instance.follower_user)
                    follower_name_en = follower_name
                elif instance.follower_artist:
                    follower_name = instance.follower_artist.name
                    follower_name_en = instance.follower_artist.name_en or follower_name
                else:
                    follower_name = "یک کاربر"
                    follower_name_en = "A user"

                text = f"{follower_name} شروع به دنبال کردن شما کرد."
                text_en = f"{follower_name_en} started following you."
                _send_or_update_notification(target_user, text, text_en)
        except Exception:
            pass

@receiver(m2m_changed, sender=UserPlaylist.liked_by.through)
def notify_playlist_like(sender, instance, action, pk_set, **kwargs):
    """Notify playlist owner when someone likes their playlist."""
    if action == "post_add":
        owner = instance.user
        try:
            setting, _ = NotificationSetting.objects.get_or_create(user=owner)
            if setting.new_likes:
                for pk in pk_set:
                    if pk != owner.id:
                        try:
                            liker = User.objects.get(pk=pk)
                            liker_name = _get_user_display_name(liker)
                            text = f"{liker_name} لیست پخش '{instance.title}' شما را لایک کرد."
                            text_en = f"{liker_name} liked your playlist '{instance.title}'."
                            _send_or_update_notification(owner, text, text_en)
                        except User.DoesNotExist:
                            continue
        except Exception:
            pass

@receiver(pre_save, sender=Song)
def capture_old_song_status(sender, instance, **kwargs):
    """Capture the status of a song before saving to detect changes."""
    if instance.pk:
        try:
            old_obj = Song.objects.get(pk=instance.pk)
            instance._old_status = old_obj.status
        except Song.DoesNotExist:
            instance._old_status = None
    else:
        instance._old_status = None

@receiver(post_save, sender=Song)
def notify_new_song_published(sender, instance, created, **kwargs):
    """Notify followers when a song is published."""
    old_status = getattr(instance, '_old_status', None)
    
    # Trigger notification if status just changed to published (or created as published)
    if instance.status == Song.STATUS_PUBLISHED and old_status != Song.STATUS_PUBLISHED:
        artist = instance.artist
        # Find all users following this artist
        followers = Follow.objects.filter(followed_artist=artist).select_related('follower_user')
        for follow in followers:
            if follow.follower_user:
                user = follow.follower_user
                try:
                    setting, _ = NotificationSetting.objects.get_or_create(user=user)
                    if setting.new_song_followed_artists:
                        text = f"آهنگ جدید '{instance.title}' از {artist.name} منتشر شد!"
                        text_en = f"New song '{instance.title_en or instance.title}' by {artist.name_en or artist.name} is out!"
                        _send_or_update_notification(user, text, text_en)
                except Exception:
                    pass

@receiver(post_save, sender=Album)
def notify_new_album_published(sender, instance, created, **kwargs):
    """Notify followers when a new album is released."""
    if created:
        artist = instance.artist
        # Find all users following this artist
        followers = Follow.objects.filter(followed_artist=artist).select_related('follower_user')
        for follow in followers:
            if follow.follower_user:
                user = follow.follower_user
                try:
                    setting, _ = NotificationSetting.objects.get_or_create(user=user)
                    if setting.new_album_followed_artists:
                        text = f"آلبوم جدید '{instance.title}' از {artist.name} منتشر شد!"
                        text_en = f"New album '{instance.title_en or instance.title}' by {artist.name_en or artist.name} is out!"
                        _send_or_update_notification(user, text, text_en)
                except Exception:
                    pass

# Keep cached similarity rankings fresh without caching response payloads.
from .performance import (
    AFFINITY_VERSION_KEY,
    CATALOG_VERSION_KEY,
    USER_DIRECTORY_VERSION_KEY,
    cache_increment,
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
