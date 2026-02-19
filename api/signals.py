from django.db.models.signals import post_save, m2m_changed, pre_save
from django.dispatch import receiver
from .models import User, Song, Album, Follow, UserPlaylist, Notification, NotificationSetting
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

def _send_or_update_notification(user_or_artist, text):
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
        Notification.objects.filter(pk=existing.pk).update(has_read=False, created_at=now)
    else:
        Notification.objects.create(**lookup)

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
                elif instance.follower_artist:
                    follower_name = instance.follower_artist.name
                else:
                    follower_name = "یک کاربر"

                text = f"{follower_name} شروع به دنبال کردن شما کرد."
                _send_or_update_notification(target_user, text)
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
                            _send_or_update_notification(owner, text)
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
                        _send_or_update_notification(user, text)
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
                        _send_or_update_notification(user, text)
                except Exception:
                    pass
