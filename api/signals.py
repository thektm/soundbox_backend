from django.db.models.signals import post_save, m2m_changed, pre_save
from django.dispatch import receiver
from .models import User, Song, Album, Follow, UserPlaylist, Notification, NotificationSetting
from django.utils.translation import gettext as _

@receiver(post_save, sender=User)
def create_user_notification_settings(sender, instance, created, **kwargs):
    """Automatically create notification settings for new users."""
    if created:
        NotificationSetting.objects.get_or_create(user=instance)

@receiver(post_save, sender=Follow)
def notify_new_follower(sender, instance, created, **kwargs):
    """Notify a user when someone starts following them."""
    if created and instance.followed_user:
        user = instance.followed_user
        try:
            # Refresh from DB to ensure setting exists
            setting, _ = NotificationSetting.objects.get_or_create(user=user)
            if setting.new_follower:
                # Resolve a friendly display name for the follower:
                def _display_name_for_user(u: User):
                    if not u:
                        return "یک کاربر"
                    # Prefer unique_id, then first+last name, then fallback
                    if getattr(u, 'unique_id', None):
                        return u.unique_id
                    names = " ".join(filter(None, [getattr(u, 'first_name', '') or '', getattr(u, 'last_name', '') or ''])).strip()
                    if names:
                        return names
                    return "یک کاربر"

                if instance.follower_user:
                    follower_name = _display_name_for_user(instance.follower_user)
                elif instance.follower_artist:
                    follower_name = instance.follower_artist.name
                else:
                    follower_name = "یک کاربر"

                Notification.objects.create(
                    user=user,
                    text=f"{follower_name} شروع به دنبال کردن شما کرد."
                )
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
                def _display_name_for_user_by_pk(pk):
                    try:
                        u = User.objects.get(pk=pk)
                        if getattr(u, 'unique_id', None):
                            return u.unique_id
                        names = " ".join(filter(None, [getattr(u, 'first_name', '') or '', getattr(u, 'last_name', '') or ''])).strip()
                        if names:
                            return names
                        return "یک کاربر"
                    except User.DoesNotExist:
                        return "یک کاربر"

                for pk in pk_set:
                    if pk != owner.id:
                        liker_name = _display_name_for_user_by_pk(pk)
                        Notification.objects.create(
                            user=owner,
                            text=f"{liker_name} لیست پخش '{instance.title}' شما را لایک کرد."
                        )
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
                        Notification.objects.create(
                            user=user,
                            text=f"آهنگ جدید '{instance.title}' از {artist.name} منتشر شد!"
                        )
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
                        Notification.objects.create(
                            user=user,
                            text=f"آلبوم جدید '{instance.title}' از {artist.name} منتشر شد!"
                        )
                except Exception:
                    pass
