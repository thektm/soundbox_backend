from rest_framework import serializers
from .utils import generate_signed_r2_url
from .models import (
    User, UserPlaylist, Artist, ArtistSocialAccount , ArtistAuth, RefreshToken, EventPlaylist, Album, Genre, Mood, Tag, 
    SubGenre, Song, Playlist, StreamAccess, RecommendedPlaylist, SearchSection,
    NotificationSetting, Follow, SongLike, AlbumLike, PlaylistLike, Rules, PlayConfiguration,
    DepositRequest, Report, Notification, AudioAd, UserHistory
)
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from urllib.parse import urlencode
from django.urls import reverse
from django.conf import settings


class SongSummarySerializer(serializers.ModelSerializer):
    """Lightweight serializer for songs in summary views"""
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    artist_id = serializers.IntegerField(source='artist.id', read_only=True)
    album_title = serializers.CharField(source='album.title', read_only=True, allow_null=True)
    stream_url = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    genre_names = serializers.SerializerMethodField()
    tag_names = serializers.SerializerMethodField()
    mood_names = serializers.SerializerMethodField()
    sub_genre_names = serializers.SerializerMethodField()
    play_count = serializers.SerializerMethodField()

    class Meta:
        model = Song
        fields = [
            'id', 'title', 'artist_id', 'artist_name', 'album_title', 'cover_image', 
            'stream_url', 'duration_seconds', 'is_liked',
            'genre_names', 'tag_names', 'mood_names', 'sub_genre_names', 'play_count'
        ]

    def get_genre_names(self, obj):
        return [g.name for g in obj.genres.all()]

    def get_tag_names(self, obj):
        return [t.name for t in obj.tags.all()]

    def get_mood_names(self, obj):
        return [m.name for m in obj.moods.all()]

    def get_sub_genre_names(self, obj):
        return [sg.name for sg in obj.sub_genres.all()]

    def get_is_liked(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            # Use prefetched data if available
            if hasattr(obj, '_prefetched_objects_cache') and 'liked_by' in obj._prefetched_objects_cache:
                return request.user in obj.liked_by.all()
            return SongLike.objects.filter(user=request.user, song=obj).exists()
        return False

    def get_stream_url(self, obj):
        # Reuse the logic but maybe we can optimize it later
        # For now, let's keep it consistent with SongStreamSerializer but without the ad logic for speed
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            import secrets
            short_token = secrets.token_urlsafe(6)[:8]
            unique_otplay_id = secrets.token_urlsafe(16)
            
            StreamAccess.objects.create(
                user=request.user,
                song=obj,
                short_token=short_token,
                unique_otplay_id=unique_otplay_id
            )
            
            from django.urls import reverse
            short_path = reverse('stream-short', kwargs={'token': short_token})
            return request.build_absolute_uri(short_path)
        return None

    def get_play_count(self, obj):
        """Return the total play count as an integer"""
        try:
            return (obj.plays or 0) + obj.play_counts.count()
        except Exception:
            return int(obj.plays or 0)


class ArtistSummarySerializer(serializers.ModelSerializer):
    """Lightweight serializer for artists in summary views"""
    is_following = serializers.SerializerMethodField()
    followers_count = serializers.SerializerMethodField()

    class Meta:
        model = Artist
        fields = ['id', 'name', 'profile_image', 'is_following', 'verified', 'followers_count']

    def get_followers_count(self, obj):
        if hasattr(obj, '_prefetched_objects_cache') and 'follower_artist_relations' in obj._prefetched_objects_cache:
            return len(obj.follower_artist_relations.all())
        return Follow.objects.filter(followed_artist=obj).count()

    def get_is_following(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            if hasattr(obj, '_prefetched_objects_cache') and 'follower_artist_relations' in obj._prefetched_objects_cache:
                return any(f.follower_user_id == request.user.id for f in obj.follower_artist_relations.all())
            return Follow.objects.filter(follower_user=request.user, followed_artist=obj).exists()
        return False


class AlbumSummarySerializer(serializers.ModelSerializer):
    """Lightweight serializer for albums in summary views"""
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    is_liked = serializers.SerializerMethodField()
    genre_names = serializers.SerializerMethodField()
    mood_names = serializers.SerializerMethodField()
    sub_genre_names = serializers.SerializerMethodField()
    cover_image = serializers.SerializerMethodField()

    class Meta:
        model = Album
        fields = ['id', 'title', 'artist_name', 'cover_image', 'is_liked', 'genre_names', 'mood_names', 'sub_genre_names']

    def get_cover_image(self, obj):
        if obj.cover_image:
            return obj.cover_image
        # Fallback to the first song's cover if available
        try:
            songs = obj.songs.all()
            first_song = songs[0] if songs else None
            if first_song:
                return first_song.cover_image
        except Exception:
            pass
        return None

    def get_genre_names(self, obj):
        # Extract from album directly
        names = set(g.name for g in obj.genres.all())
        # Aggregate from all songs in this album
        try:
            for song in obj.songs.all():
                for g in song.genres.all():
                    names.add(g.name)
        except Exception:
            pass
        return list(names)

    def get_mood_names(self, obj):
        # Extract from album directly
        names = set(m.name for m in obj.moods.all())
        # Aggregate from all songs in this album
        try:
            for song in obj.songs.all():
                for m in song.moods.all():
                    names.add(m.name)
        except Exception:
            pass
        return list(names)

    def get_sub_genre_names(self, obj):
        # Extract from album directly
        names = set(sg.name for sg in obj.sub_genres.all())
        # Aggregate from all songs in this album
        try:
            for song in obj.songs.all():
                for sg in song.sub_genres.all():
                    names.add(sg.name)
        except Exception:
            pass
        return list(names)

    def get_is_liked(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            if hasattr(obj, '_prefetched_objects_cache') and 'liked_by' in obj._prefetched_objects_cache:
                return request.user in obj.liked_by.all()
            return AlbumLike.objects.filter(user=request.user, album=obj).exists()
        return False


class PlaylistSummarySerializer(serializers.ModelSerializer):
    """Lightweight serializer for playlists in summary views"""
    songs_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    cover_image = serializers.SerializerMethodField()
    top_three_song_covers = serializers.SerializerMethodField()
    genre_names = serializers.SerializerMethodField()
    mood_names = serializers.SerializerMethodField()
    type = serializers.ReadOnlyField(default='recommended')

    class Meta:
        model = RecommendedPlaylist
        fields = [
            'id', 'unique_id', 'title', 'description', 'cover_image', 
            'top_three_song_covers', 'songs_count', 'is_liked', 'genre_names', 
            'mood_names', 'type'
        ]

    def get_genre_names(self, obj):
        """Aggregate unique genre names from all songs in this playlist."""
        try:
            # Try to use prefetched data
            names = set()
            for song in obj.songs.all():
                for g in song.genres.all():
                    names.add(g.name)
            return list(names)
        except Exception:
            return []

    def get_mood_names(self, obj):
        """Aggregate unique mood names from all songs in this playlist."""
        try:
            # Try to use prefetched data
            names = set()
            for song in obj.songs.all():
                for m in song.moods.all():
                    names.add(m.name)
            return list(names)
        except Exception:
            return []

    def get_top_three_song_covers(self, obj):
        """Return the cover images of the first 3 songs in the playlist."""
        try:
            # Use prefetched songs if available
            songs = list(obj.songs.all()[:3])
            covers = []
            for s in songs:
                cover = getattr(s, 'cover_image', None) or (getattr(s, 'album', None) and getattr(s.album, 'cover_image', None))
                if cover:
                    covers.append(cover)
            return covers
        except Exception:
            return []

    def get_cover_image(self, obj):
        # RecommendedPlaylist doesn't have cover_image, but its playlist_ref might
        if obj.playlist_ref and obj.playlist_ref.cover_image:
            return obj.playlist_ref.cover_image
        # Fallback to the first song's cover if available
        first_song = obj.songs.first()
        if first_song:
            return first_song.cover_image
        return None

    def get_songs_count(self, obj):
        if hasattr(obj, 'songs'):
            return obj.songs.count()
        return 0

    def get_is_liked(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            if hasattr(obj, '_prefetched_objects_cache') and 'liked_by' in obj._prefetched_objects_cache:
                return request.user in obj.liked_by.all()
            return obj.liked_by.filter(id=request.user.id).exists()
        return False


class SimplePlaylistSerializer(serializers.ModelSerializer):
    """Summary serializer for normal Playlist model used in history/library"""
    songs_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    cover_image = serializers.SerializerMethodField()
    top_three_song_covers = serializers.SerializerMethodField()
    genre_names = serializers.SerializerMethodField()
    mood_names = serializers.SerializerMethodField()
    type = serializers.ReadOnlyField(default='normal-playlist')

    class Meta:
        model = Playlist
        fields = [
            'id', 'title', 'description', 'cover_image', 
            'top_three_song_covers', 'songs_count', 'is_liked', 'genre_names', 
            'mood_names', 'type'
        ]

    def get_genre_names(self, obj):
        return [g.name for g in obj.genres.all()]

    def get_mood_names(self, obj):
        return [m.name for m in obj.moods.all()]

    def get_top_three_song_covers(self, obj):
        try:
            songs = list(obj.songs.all()[:3])
            covers = []
            for s in songs:
                cover = s.cover_image or (s.album and s.album.cover_image)
                if cover:
                    covers.append(cover)
            return covers
        except Exception:
            return []

    def get_songs_count(self, obj):
        return obj.songs.count()

    def get_is_liked(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return obj.liked_by.filter(id=request.user.id).exists()
        return False

    def get_cover_image(self, obj):
        if obj.cover_image:
            return obj.cover_image
        first_song = obj.songs.first()
        if first_song:
            return first_song.cover_image
        return None


class FollowableEntitySerializer(serializers.Serializer):
    """Unified serializer for both User and Artist in follow lists"""
    id = serializers.IntegerField()
    type = serializers.SerializerMethodField()
    name = serializers.SerializerMethodField()
    image = serializers.SerializerMethodField()
    is_verified = serializers.SerializerMethodField()
    followers_count = serializers.SerializerMethodField()
    following_count = serializers.SerializerMethodField()
    is_following = serializers.SerializerMethodField()

    def get_type(self, obj):
        return 'artist' if isinstance(obj, Artist) else 'user'

    def get_name(self, obj):
        if isinstance(obj, Artist):
            return obj.name
        name = f"{obj.first_name} {obj.last_name}".strip()
        return name if name else obj.phone_number

    def get_image(self, obj):
        if isinstance(obj, Artist):
            return obj.profile_image
        # Users might store profile image in settings or we can return empty
        return obj.settings.get('profile_image', '') if isinstance(obj.settings, dict) else ''

    def get_is_verified(self, obj):
        if isinstance(obj, Artist):
            return obj.verified
        return obj.is_verified

    def get_followers_count(self, obj):
        if isinstance(obj, Artist):
            return Follow.objects.filter(followed_artist=obj).count()
        return Follow.objects.filter(followed_user=obj).count()

    def get_following_count(self, obj):
        if isinstance(obj, Artist):
            return Follow.objects.filter(follower_artist=obj).count()
        return Follow.objects.filter(follower_user=obj).count()

    def get_is_following(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return False
        if isinstance(obj, Artist):
            return Follow.objects.filter(follower_user=request.user, followed_artist=obj).exists()
        return Follow.objects.filter(follower_user=request.user, followed_user=obj).exists()


class FollowRequestSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(required=False)
    artist_id = serializers.IntegerField(required=False)

    def validate(self, data):
        if not data.get('user_id') and not data.get('artist_id'):
            raise serializers.ValidationError("Either user_id or artist_id must be provided.")
        if data.get('user_id') and data.get('artist_id'):
            raise serializers.ValidationError("Only one of user_id or artist_id should be provided.")
        return data


class NotificationSettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationSetting
        fields = [
            'new_song_followed_artists', 'new_album_followed_artists', 
            'new_playlist', 'new_likes', 'new_follower', 'system_notifications'
        ]


class UserSerializer(serializers.ModelSerializer):
    followers_count = serializers.SerializerMethodField()
    following_count = serializers.SerializerMethodField()
    user_playlists_count = serializers.IntegerField(source='user_playlists.count', read_only=True)
    recently_played = serializers.SerializerMethodField()
    notification_setting = NotificationSettingSerializer(read_only=True)
    followers = serializers.SerializerMethodField()
    following = serializers.SerializerMethodField()
    
    class Meta:
        model = get_user_model()
        fields = [
            'id', 'phone_number', 'first_name', 'last_name', 'email',
            'roles', 'is_active', 'is_staff', 'date_joined',
            'followers_count', 'following_count', 'user_playlists_count', 
            'recently_played', 'notification_setting', 'plan', 'stream_quality',
            'followers', 'following'
        ]
        read_only_fields = [
            'id', 'is_active', 'is_staff', 'date_joined', 
            'followers_count', 'following_count', 'user_playlists_count',
            'notification_setting', 'followers', 'following', 'plan'
        ]

    def get_followers_count(self, obj):
        return Follow.objects.filter(followed_user=obj).count()

    def get_following_count(self, obj):
        return Follow.objects.filter(follower_user=obj).count()

    def get_followers(self, obj):
        request = self.context.get('request')
        page, page_size = 1, 10
        if request:
            try:
                page = int(request.query_params.get('f_page', 1))
                page_size = int(request.query_params.get('f_page_size', 10))
            except (ValueError, TypeError): pass
        
        offset = (page - 1) * page_size
        qs = Follow.objects.filter(followed_user=obj).order_by('-created_at')
        total = qs.count()
        items = [f.follower_user or f.follower_artist for f in qs[offset:offset + page_size]]
        has_next = total > offset + page_size
        next_url = None
        if request and has_next:
            # prefer stable named route for profile lists
            try:
                base = reverse('user_profile')
            except Exception:
                base = request.path
            params = {k: str(v) for k, v in request.query_params.items()}
            params['f_page'] = str(page + 1)
            params['f_page_size'] = str(page_size)
            qs = urlencode(params)
            try:
                next_url = request.build_absolute_uri(base + '?' + qs)
            except Exception:
                # fallback to settings if available
                site = getattr(settings, 'SITE_URL', None)
                if site:
                    next_url = site.rstrip('/') + base + '?' + qs
                else:
                    try:
                        scheme = 'https' if getattr(request, 'is_secure', lambda: False)() else 'http'
                        host = request.get_host()
                        next_url = f"{scheme}://{host}{base}?{qs}"
                    except Exception:
                        next_url = None

        return {
            'items': FollowableEntitySerializer(items, many=True, context=self.context).data,
            'total': total,
            'page': page,
            'has_next': has_next,
            'next': next_url,
        }

    def get_following(self, obj):
        request = self.context.get('request')
        page, page_size = 1, 10
        if request:
            try:
                page = int(request.query_params.get('fg_page', 1))
                page_size = int(request.query_params.get('fg_page_size', 10))
            except (ValueError, TypeError): pass
        
        offset = (page - 1) * page_size
        qs = Follow.objects.filter(follower_user=obj).order_by('-created_at')
        total = qs.count()
        items = [f.followed_user or f.followed_artist for f in qs[offset:offset + page_size]]
        has_next = total > offset + page_size
        next_url = None
        if request and has_next:
            try:
                base = reverse('user_profile')
            except Exception:
                base = request.path
            params = {k: str(v) for k, v in request.query_params.items()}
            params['fg_page'] = str(page + 1)
            params['fg_page_size'] = str(page_size)
            qs = urlencode(params)
            try:
                next_url = request.build_absolute_uri(base + '?' + qs)
            except Exception:
                site = getattr(settings, 'SITE_URL', None)
                if site:
                    next_url = site.rstrip('/') + base + '?' + qs
                else:
                    try:
                        scheme = 'https' if getattr(request, 'is_secure', lambda: False)() else 'http'
                        host = request.get_host()
                        next_url = f"{scheme}://{host}{base}?{qs}"
                    except Exception:
                        next_url = None

        return {
            'items': FollowableEntitySerializer(items, many=True, context=self.context).data,
            'total': total,
            'page': page,
            'has_next': has_next,
            'next': next_url,
        }

    def validate_stream_quality(self, value):
        user = self.instance
        if value == 'high' and user.plan != 'premium':
            raise serializers.ValidationError("High quality streaming is only available for premium users.")
        return value

    def update(self, instance, validated_data):
        # Handle nested notification_setting update
        notification_data = self.context['request'].data.get('notification_setting')
        if notification_data:
            # Ensure the user has a notification setting record
            notification_setting, created = NotificationSetting.objects.get_or_create(user=instance)
            ns_serializer = NotificationSettingSerializer(notification_setting, data=notification_data, partial=True)
            if ns_serializer.is_valid():
                ns_serializer.save()
        
        return super().update(instance, validated_data)

    def get_recently_played(self, obj):
        # Get unique songs recently played by this user, ordered by latest play
        from .models import Song
        from django.db.models import Max
        
        request = self.context.get('request')
        page = 1
        page_size = 10
        if request:
            try:
                page = int(request.query_params.get('rp_page', 1))
                page_size = int(request.query_params.get('rp_page_size', 10))
            except (ValueError, TypeError):
                pass

        offset = (page - 1) * page_size
        
        # Annotate each song with its latest play time for this user
        qs = Song.objects.filter(play_counts__user=obj).annotate(
            latest_play=Max('play_counts__created_at')
        ).order_by('-latest_play')
        
        total = qs.count()
        songs = qs[offset:offset + page_size]
        has_next = total > offset + page_size
        next_url = None
        if request and has_next:
            try:
                base = reverse('user_history_list')
            except Exception:
                base = request.path
            params = {k: str(v) for k, v in request.query_params.items()}
            params['rp_page'] = str(page + 1)
            params['rp_page_size'] = str(page_size)
            qs = urlencode(params)
            try:
                next_url = request.build_absolute_uri(base + '?' + qs)
            except Exception:
                site = getattr(settings, 'SITE_URL', None)
                if site:
                    next_url = site.rstrip('/') + base + '?' + qs
                else:
                    try:
                        scheme = 'https' if getattr(request, 'is_secure', lambda: False)() else 'http'
                        host = request.get_host()
                        next_url = f"{scheme}://{host}{base}?{qs}"
                    except Exception:
                        next_url = None

        return {
            'items': SongStreamSerializer(songs, many=True, context=self.context).data,
            'total': total,
            'page': page,
            'has_next': has_next,
            'next': next_url,
        }


class UserHistorySerializer(serializers.ModelSerializer):
    """Serializer for user history items with flattened content"""
    type = serializers.CharField(source='content_type')
    item = serializers.SerializerMethodField()

    class Meta:
        model = UserHistory
        fields = ['id', 'type', 'item', 'updated_at']

    def get_item(self, obj):
        request = self.context.get('request')
        if obj.content_type == UserHistory.TYPE_SONG and obj.song:
            return SongSummarySerializer(obj.song, context={'request': request}).data
        elif obj.content_type == UserHistory.TYPE_ALBUM and obj.album:
            return AlbumSummarySerializer(obj.album, context={'request': request}).data
        elif obj.content_type == UserHistory.TYPE_PLAYLIST and obj.playlist:
            return SimplePlaylistSerializer(obj.playlist, context={'request': request}).data
        elif obj.content_type == UserHistory.TYPE_ARTIST and obj.artist:
            return ArtistSummarySerializer(obj.artist, context={'request': request}).data
        return None


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)
    # allow callers to request artist role at registration time (boolean)
    artist = serializers.BooleanField(write_only=True, required=False)
    artistPassword = serializers.CharField(write_only=True, required=False)

    playlists = serializers.JSONField(required=False)
    settings = serializers.JSONField(required=False)

    class Meta:
        model = User
        # Do NOT allow clients to set `roles` directly via this serializer.
        fields = ['phone_number', 'password', 'first_name', 'last_name', 'email', 'playlists', 'plan', 'settings', 'artist', 'artistPassword']

    def validate_phone_number(self, value):
        if User.objects.filter(phone_number=value).exists():
            raise serializers.ValidationError('A user with that phone number already exists')
        return value

    def create(self, validated_data):
        password = validated_data.pop('password')
        artist_flag = validated_data.pop('artist', False)
        artist_password = validated_data.pop('artistPassword', None)

        create_kwargs = {}
        if artist_flag:
            create_kwargs['roles'] = [User.ROLE_AUDIENCE, User.ROLE_ARTIST]
        else:
            create_kwargs['roles'] = [User.ROLE_AUDIENCE]

        if artist_password:
            create_kwargs['artist_password'] = artist_password

        user = User.objects.create_user(password=password, **{**validated_data, **create_kwargs})
        return user


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        data = super().validate(attrs)
        # attach user profile but exclude internal flags from the login response
        user_data = UserSerializer(self.user).data
        # remove `is_staff` so it doesn't appear in the token response
        user_data.pop('is_staff', None)
        data['user'] = user_data
        return data


class UploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    folder = serializers.CharField(required=False, allow_blank=True)
    filename = serializers.CharField(required=False, allow_blank=True)


# --- Auth related serializers ---
class PhoneSerializer(serializers.Serializer):
    phone = serializers.CharField()


class RegisterRequestSerializer(serializers.Serializer):
    phone = serializers.CharField()
    password = serializers.CharField(write_only=True)
    # `artist` flag is taken from query params now; password used as artist password when artist=true


class VerifySerializer(serializers.Serializer):
    phone = serializers.CharField()
    otp = serializers.CharField()


class LoginPasswordSerializer(serializers.Serializer):
    phone = serializers.CharField()
    password = serializers.CharField(write_only=True)


class LoginOtpRequestSerializer(serializers.Serializer):
    phone = serializers.CharField()


class LoginOtpVerifySerializer(serializers.Serializer):
    phone = serializers.CharField()
    otp = serializers.CharField()


class ForgotPasswordSerializer(serializers.Serializer):
    phone = serializers.CharField()


class PasswordResetSerializer(serializers.Serializer):
    phone = serializers.CharField(required=False)
    otp = serializers.CharField(required=False)
    newPassword = serializers.CharField(write_only=True)
    resetToken = serializers.CharField(required=False)


class TokenRefreshRequestSerializer(serializers.Serializer):
    refreshToken = serializers.CharField()

class ArtistSocialAccountSerializer(serializers.ModelSerializer):
    platform_name = serializers.CharField(source='platform.name', read_only=True)
    platform_slug = serializers.CharField(source='platform.slug', read_only=True)
    platform_base_url = serializers.CharField(source='platform.base_url', read_only=True)

    class Meta:
        model = ArtistSocialAccount
        fields = [
            'id', 'platform', 'platform_name', 'platform_slug', 'platform_base_url',
            'username', 'url', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'platform_name', 'platform_slug', 'platform_base_url']
class LogoutSerializer(serializers.Serializer):
    refreshToken = serializers.CharField()


class ChangePasswordSerializer(serializers.Serializer):
    currentPassword = serializers.CharField(write_only=True)
    newPassword = serializers.CharField(write_only=True)


class ArtistSerializer(serializers.ModelSerializer):
    """Serializer for Artist model"""
    user_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), 
        source='user', 
        required=False, 
        allow_null=True
    )
    followers_count = serializers.SerializerMethodField()
    followings_count = serializers.SerializerMethodField()
    monthly_listeners_count = serializers.SerializerMethodField()
    live_listeners = serializers.ReadOnlyField()
    is_following = serializers.SerializerMethodField()
    followers = serializers.SerializerMethodField()
    following = serializers.SerializerMethodField()
    social_accounts = ArtistSocialAccountSerializer(many=True, read_only=True, source='social_account_links')
    
    class Meta:
        model = Artist
        fields = [
            'id', 'name', 'artistic_name', 'user_id', 'bio', 'profile_image', 'banner_image', 
            'email', 'city', 'date_of_birth', 'address', 'id_number',
            'verified', 'followers_count', 'followings_count', 
            'monthly_listeners_count', 'live_listeners', 'is_following', 'created_at',
            'followers', 'following', 'social_accounts'
        ]
        read_only_fields = [
            'id', 'created_at', 'followers_count', 'followings_count', 
            'monthly_listeners_count', 'live_listeners', 'is_following', 'followers', 'following', 'social_accounts'
        ]

    def get_followers_count(self, obj):
        return Follow.objects.filter(followed_artist=obj).count()

    def get_followings_count(self, obj):
        return Follow.objects.filter(follower_artist=obj).count()

    def get_followers(self, obj):
        request = self.context.get('request')
        page, page_size = 1, 10
        if request:
            try:
                page = int(request.query_params.get('f_page', 1))
                page_size = int(request.query_params.get('f_page_size', 10))
            except (ValueError, TypeError): pass
        
        offset = (page - 1) * page_size
        qs = Follow.objects.filter(followed_artist=obj).order_by('-created_at')
        total = qs.count()
        items = [f.follower_user or f.follower_artist for f in qs[offset:offset + page_size]]
        
        return {
            'items': FollowableEntitySerializer(items, many=True, context=self.context).data,
            'total': total,
            'page': page,
            'has_next': total > offset + page_size
        }

    def get_following(self, obj):
        request = self.context.get('request')
        page, page_size = 1, 10
        if request:
            try:
                page = int(request.query_params.get('fg_page', 1))
                page_size = int(request.query_params.get('fg_page_size', 10))
            except (ValueError, TypeError): pass
        
        offset = (page - 1) * page_size
        qs = Follow.objects.filter(follower_artist=obj).order_by('-created_at')
        total = qs.count()
        items = [f.followed_user or f.followed_artist for f in qs[offset:offset + page_size]]
        
        return {
            'items': FollowableEntitySerializer(items, many=True, context=self.context).data,
            'total': total,
            'page': page,
            'has_next': total > offset + page_size
        }

    def get_monthly_listeners_count(self, obj):
        from django.utils import timezone
        from datetime import timedelta
        # Count unique users who listened in the last 28 days
        cutoff = timezone.now() - timedelta(days=28)
        return obj.monthly_listener_records.filter(updated_at__gte=cutoff).count()

    def get_is_following(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return Follow.objects.filter(follower_user=request.user, followed_artist=obj).exists()
        return False


class PopularArtistSerializer(ArtistSerializer):
    """Artist serializer extended with popularity metrics."""
    total_plays = serializers.IntegerField(read_only=True)
    total_likes = serializers.IntegerField(read_only=True)
    total_playlist_adds = serializers.IntegerField(read_only=True)

    class Meta(ArtistSerializer.Meta):
        fields = ArtistSerializer.Meta.fields + ['total_plays', 'total_likes', 'total_playlist_adds']


class ArtistAuthSerializer(serializers.ModelSerializer):
    """Serializer for ArtistAuth verification submissions."""
    user_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), source='user', required=False, allow_null=True
    )
    national_id_image = serializers.ImageField(required=True)
    artist_claimed = serializers.PrimaryKeyRelatedField(
        queryset=Artist.objects.all(), required=False, allow_null=True
    )

    class Meta:
        model = ArtistAuth
        fields = [
            'id', 'user_id', 'auth_type', 'artist_claimed', 'first_name', 'last_name', 'stage_name',
            'birth_date', 'national_id', 'phone_number', 'email', 'city', 'address',
            'biography', 'national_id_image', 'is_verified', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'is_verified', 'status', 'created_at', 'updated_at']

    def create(self, validated_data):
        request = self.context.get('request')
        if request and getattr(request, 'user', None) and request.user.is_authenticated:
            # prefer the authenticated user over supplied user_id
            validated_data['user'] = request.user
        return super().create(validated_data)

    def validate(self, data):
        # If author is claiming an existing artist, require artist_claimed
        auth_type = data.get('auth_type') or getattr(self.instance, 'auth_type', None)
        artist_claimed = data.get('artist_claimed') or getattr(self.instance, 'artist_claimed', None)
        from .models import ArtistAuth
        if auth_type == ArtistAuth.AUTH_EXISTING and not artist_claimed:
            raise serializers.ValidationError({'artist_claimed': 'This field is required when auth_type is existing_artist.'})
        return data

    def update(self, instance, validated_data):
        return super().update(instance, validated_data)
    def update(self, instance, validated_data):
        return super().update(instance, validated_data)


class AlbumSerializer(serializers.ModelSerializer):
    """Serializer for Album model"""
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    artist_id = serializers.IntegerField(source='artist.id', read_only=True)
    likes_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    
    # For write operations
    genre_ids_write = serializers.PrimaryKeyRelatedField(
        queryset=Genre.objects.all(), 
        many=True, 
        source='genres', 
        required=False,
        write_only=True
    )
    sub_genre_ids_write = serializers.PrimaryKeyRelatedField(
        queryset=SubGenre.objects.all(), 
        many=True, 
        source='sub_genres', 
        required=False,
        write_only=True
    )
    mood_ids_write = serializers.PrimaryKeyRelatedField(
        queryset=Mood.objects.all(), 
        many=True, 
        source='moods', 
        required=False,
        write_only=True
    )

    # Read-only fields with titles
    genre_ids = serializers.SerializerMethodField()
    sub_genre_ids = serializers.SerializerMethodField()
    mood_ids = serializers.SerializerMethodField()
    # Include songs in album detail and aggregated genres/moods from songs
    songs = serializers.SerializerMethodField()
    song_genre_names = serializers.SerializerMethodField()
    song_mood_names = serializers.SerializerMethodField()

    class Meta:
        model = Album
        fields = [
            'id', 'title', 'artist_id', 'artist_name', 'cover_image', 
            'release_date', 'description', 'created_at', 'likes_count', 'is_liked',
            'genre_ids_write', 'sub_genre_ids_write', 'mood_ids_write',
            'genre_ids', 'sub_genre_ids', 'mood_ids'
        ]
        # expose songs and aggregated song-level genres/moods for detail views
        # appended at the end to avoid breaking clients that depend on field order
        fields += ['songs', 'song_genre_names', 'song_mood_names']
        read_only_fields = ['id', 'created_at', 'likes_count', 'is_liked']

    def get_likes_count(self, obj):
        return AlbumLike.objects.filter(album=obj).count()

    def get_is_liked(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return AlbumLike.objects.filter(user=request.user, album=obj).exists()

    def get_genre_ids(self, obj):
        return [genre.name for genre in obj.genres.all()]

    def get_sub_genre_ids(self, obj):
        return [sub_genre.name for sub_genre in obj.sub_genres.all()]

    def get_mood_ids(self, obj):
        return [mood.name for mood in obj.moods.all()]

    def get_songs(self, obj):
        try:
            # include only published songs related to this album
            qs = obj.songs.all().select_related('artist', 'album')
            # Use SongStreamSerializer so stream URLs are wrapper links (consistent with other detail views)
            try:
                serializer_cls = globals().get('SongStreamSerializer')
                if serializer_cls:
                    return serializer_cls(qs, many=True, context=self.context).data
            except Exception:
                pass
            # fallback minimal representation
            return [
                {
                    'id': s.id,
                    'title': s.title,
                    'artist_id': getattr(s.artist, 'id', None),
                    'artist_name': getattr(s.artist, 'name', None),
                }
                for s in qs
            ]
        except Exception:
            return []

    def get_song_genre_names(self, obj):
        try:
            names = set()
            for s in obj.songs.prefetch_related('genres').all():
                for g in s.genres.all():
                    names.add(g.name)
            return list(names)
        except Exception:
            return []

    def get_song_mood_names(self, obj):
        try:
            names = set()
            for s in obj.songs.prefetch_related('moods').all():
                for m in s.moods.all():
                    names.add(m.name)
            return list(names)
        except Exception:
            return []

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        if not representation.get('cover_image'):
            # Fallback to the cover image of the first song in the album if the album cover is missing
            first_song = instance.songs.all().first()
            if first_song and first_song.cover_image:
                representation['cover_image'] = first_song.cover_image
        return representation


class PopularAlbumSerializer(AlbumSerializer):
    """Album serializer with popularity metrics and top 3 song cover URLs."""
    total_song_plays = serializers.IntegerField(read_only=True)
    total_song_likes = serializers.IntegerField(read_only=True)
    album_likes = serializers.IntegerField(read_only=True)
    total_playlist_adds = serializers.IntegerField(read_only=True)
    weekly_plays = serializers.IntegerField(read_only=True)
    daily_plays = serializers.IntegerField(read_only=True)
    score = serializers.IntegerField(read_only=True)
    top_song_covers = serializers.SerializerMethodField()

    class Meta(AlbumSerializer.Meta):
        fields = AlbumSerializer.Meta.fields + [
            'total_song_plays', 'total_song_likes', 'album_likes',
            'total_playlist_adds', 'weekly_plays', 'daily_plays', 'score', 'top_song_covers'
        ]

    def get_top_song_covers(self, obj):
        # obj.songs may be prefetched by the view; choose first 3
        try:
            # Use prefetched songs if available to avoid N+1 queries
            songs = list(obj.songs.all())[:3]
            return [s.cover_image for s in songs if s.cover_image]
        except Exception:
            return []


class GenreSerializer(serializers.ModelSerializer):
    """Serializer for Genre model"""
    class Meta:
        model = Genre
        fields = ['id', 'name', 'slug']
        read_only_fields = ['id']


class MoodSerializer(serializers.ModelSerializer):
    """Serializer for Mood model"""
    class Meta:
        model = Mood
        fields = ['id', 'name', 'slug']
        read_only_fields = ['id']


class TagSerializer(serializers.ModelSerializer):
    """Serializer for Tag model"""
    class Meta:
        model = Tag
        fields = ['id', 'name', 'slug']
        read_only_fields = ['id']


class SubGenreSerializer(serializers.ModelSerializer):
    """Serializer for SubGenre model"""
    parent_genre_name = serializers.CharField(source='parent_genre.name', read_only=True, allow_null=True)
    
    class Meta:
        model = SubGenre
        fields = ['id', 'name', 'slug', 'parent_genre', 'parent_genre_name']
        read_only_fields = ['id']


class SlimGenreSerializer(serializers.ModelSerializer):
    title = serializers.CharField(source='name', read_only=True)

    class Meta:
        model = Genre
        fields = ['id', 'title']


class SlimMoodSerializer(serializers.ModelSerializer):
    title = serializers.CharField(source='name', read_only=True)

    class Meta:
        model = Mood
        fields = ['id', 'title']


class SlimTagSerializer(serializers.ModelSerializer):
    title = serializers.CharField(source='name', read_only=True)

    class Meta:
        model = Tag
        fields = ['id', 'title']


class SongSerializer(serializers.ModelSerializer):
    """Serializer for Song model with full details"""
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    artist_id = serializers.IntegerField(source='artist.id', read_only=True)
    album_title = serializers.CharField(source='album.title', read_only=True, allow_null=True)
    uploader_phone = serializers.CharField(source='uploader.phone_number', read_only=True, allow_null=True)
    duration_display = serializers.ReadOnlyField()
    display_title = serializers.ReadOnlyField()
    stream_url = serializers.SerializerMethodField()
    plays = serializers.SerializerMethodField()
    likes_count = serializers.SerializerMethodField()
    added_to_playlists_count = serializers.SerializerMethodField()
    added_to_playlist = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    
    # For write operations
    genre_ids_write = serializers.PrimaryKeyRelatedField(
        queryset=Genre.objects.all(), 
        many=True, 
        source='genres', 
        required=False,
        write_only=True
    )
    sub_genre_ids_write = serializers.PrimaryKeyRelatedField(
        queryset=SubGenre.objects.all(), 
        many=True, 
        source='sub_genres', 
        required=False,
        write_only=True
    )
    mood_ids_write = serializers.PrimaryKeyRelatedField(
        queryset=Mood.objects.all(), 
        many=True, 
        source='moods', 
        required=False,
        write_only=True
    )
    tag_ids_write = serializers.PrimaryKeyRelatedField(
        queryset=Tag.objects.all(), 
        many=True, 
        source='tags', 
        required=False,
        write_only=True
    )

    # Read-only fields with titles
    genre_ids = serializers.SerializerMethodField()
    sub_genre_ids = serializers.SerializerMethodField()
    mood_ids = serializers.SerializerMethodField()
    tag_ids = serializers.SerializerMethodField()

    def get_genre_ids(self, obj):
        return [{'id': genre.id, 'title': genre.name} for genre in obj.genres.all()]

    def get_sub_genre_ids(self, obj):
        return [{'id': sub_genre.id, 'title': sub_genre.name} for sub_genre in obj.sub_genres.all()]

    def get_mood_ids(self, obj):
        return [{'id': mood.id, 'title': mood.name} for mood in obj.moods.all()]

    def get_tag_ids(self, obj):
        return [{'id': tag.id, 'title': tag.name} for tag in obj.tags.all()]
    
    # Read-only paginated similar songs block
    similar_songs = serializers.SerializerMethodField()
    
    class Meta:
        model = Song
        fields = [
            'id', 'title', 'artist_id', 'artist_name', 'featured_artists',
            'album', 'album_title', 'is_single', 'stream_url', 'audio_file', 'converted_audio_url', 'cover_image',
            'original_format', 'duration_seconds', 'duration_display', 'plays',
            'likes_count', 'added_to_playlists_count', 'added_to_playlist', 'is_liked',
            'status', 'release_date', 'language', 'genre_ids', 'sub_genre_ids',
            'mood_ids', 'tag_ids', 'description', 'lyrics', 'tempo', 'energy',
            'danceability', 'valence', 'acousticness', 'instrumentalness',
            'live_performed', 'speechiness', 'label', 'producers', 'composers',
            'lyricists', 'credits', 'uploader', 'uploader_phone', 'created_at',
            'updated_at', 'display_title', 'similar_songs',
            'genre_ids_write', 'sub_genre_ids_write', 'mood_ids_write', 'tag_ids_write'
        ]
        read_only_fields = ['id', 'plays', 'likes_count', 'added_to_playlists_count', 'added_to_playlist', 'is_liked', 'created_at', 'updated_at', 'duration_display', 'display_title']

    def get_plays(self, obj):
        # Return the sum of the legacy 'plays' field and the actual PlayCount records
        try:
            return (obj.plays or 0) + obj.play_counts.count()
        except Exception:
            return obj.plays or 0

    def get_likes_count(self, obj):
        return SongLike.objects.filter(song=obj).count()

    def get_added_to_playlists_count(self, obj):
        return obj.user_playlists.count()

    def get_added_to_playlist(self, obj):
        # Count distinct users who have added this song to at least one of their playlists
        return obj.user_playlists.values('user').distinct().count()

    def get_is_liked(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return SongLike.objects.filter(user=request.user, song=obj).exists()
        return False

    def to_representation(self, instance):
        print(f"DEBUG: Serializing Song ID: {instance.id}")
        print(f"DEBUG: instance.converted_audio_url: {instance.converted_audio_url}")
        ret = super().to_representation(instance)
        # Sign URLs if they are on our CDN
        if ret.get('cover_image'):
            ret['cover_image'] = generate_signed_r2_url(ret['cover_image'])
        if ret.get('audio_file'):
            ret['audio_file'] = generate_signed_r2_url(ret['audio_file'])
        if ret.get('converted_audio_url'):
            ret['converted_audio_url'] = generate_signed_r2_url(ret['converted_audio_url'])
        print(f"DEBUG: Serialized data: {ret}")
        return ret

    def get_stream_url(self, obj):
        """
        CRITICAL: ONLY RETURN UNWRAP LINKS HERE - NEVER DIRECT SIGNED URLS!
        This endpoint MUST ONLY return short wrapper URLs that require unwrapping.
        The actual signed streaming URLs are ONLY returned by the unwrap endpoint.
        DO NOT CHANGE THIS - EVER!
        """
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            # Generate unique short token (8 characters) and avoid collisions
            import secrets
            from uuid import uuid4
            short_token = None
            for _ in range(6):
                candidate = secrets.token_urlsafe(6)[:8]
                if not StreamAccess.objects.filter(short_token=candidate).exists():
                    short_token = candidate
                    break
            if not short_token:
                short_token = uuid4().hex[:8]

            # Generate unique one-time play ID
            unique_otplay_id = None
            for _ in range(6):
                candidate = secrets.token_urlsafe(16)
                if not StreamAccess.objects.filter(unique_otplay_id=candidate).exists():
                    unique_otplay_id = candidate
                    break
            if not unique_otplay_id:
                unique_otplay_id = uuid4().hex

            # Create StreamAccess record
            StreamAccess.objects.create(
                user=request.user,
                song=obj,
                short_token=short_token,
                unique_otplay_id=unique_otplay_id
            )
            
            # Return short URL (UNWRAP LINK ONLY - NOT THE FINAL SIGNED URL!)
            from django.urls import reverse
            short_path = reverse('stream-short', kwargs={'token': short_token})
            
            # Use request to get the origin URL (where the request is coming from)
            request_obj = self.context.get('request')
            if request_obj:
                stream_url = request_obj.build_absolute_uri(short_path)
            else:
                stream_url = short_path
            
            return stream_url
        
        return None

    def get_similar_songs(self, obj):
        """Return a paginated list of similar songs with next-page link.

        Uses genres, moods, tags, artist match and audio-feature similarity to score candidates.
        Query params (on the current request) control pagination:
        - similar_page (default 1)
        - similar_page_size (default 6)
        """
        request = self.context.get('request')
        if request and '/artist/' in request.path:
            return None
        
        page = 1
        page_size = 6
        if request is not None:
            try:
                page = int(request.query_params.get('similar_page', 1))
            except Exception:
                page = 1
            try:
                page_size = int(request.query_params.get('similar_page_size', 6))
            except Exception:
                page_size = 6

        from django.db.models import Q

        base_qs = Song.objects.filter(status=Song.STATUS_PUBLISHED).exclude(id=obj.id)

        genre_ids = list(obj.genres.values_list('id', flat=True))
        mood_ids = list(obj.moods.values_list('id', flat=True))
        tag_ids = list(obj.tags.values_list('id', flat=True))

        cand_q = Q()
        if genre_ids:
            cand_q |= Q(genres__in=genre_ids)
        if mood_ids:
            cand_q |= Q(moods__in=mood_ids)
        if tag_ids:
            cand_q |= Q(tags__in=tag_ids)
        if obj.artist_id:
            cand_q |= Q(artist=obj.artist_id)

        if cand_q:
            candidates = base_qs.filter(cand_q).distinct()
        else:
            candidates = base_qs

        candidates = candidates.select_related('artist', 'album').prefetch_related('genres', 'moods', 'tags')[:500]

        # Prioritize songs from the same 10-year era (decade) based on release_date.
        era_candidates = []
        try:
            if obj.release_date and getattr(obj.release_date, 'year', None):
                y = obj.release_date.year
                era_start = (y // 10) * 10
                era_end = era_start + 9
                from django.db.models import Q
                era_q = Q(release_date__year__gte=era_start, release_date__year__lte=era_end)
                era_candidates = list(candidates.filter(era_q))
        except Exception:
            era_candidates = []

        # Build ordered candidate list: era first (if any), then remaining candidates not in era.
        era_ids = {s.id for s in era_candidates}
        remaining = [s for s in candidates if s.id not in era_ids]
        # Keep era candidates first to be scored with higher priority
        ordered_candidates = era_candidates + remaining

        # Limit to a reasonable number for scoring
        candidates = ordered_candidates[:500]

        def feature_similarity(a, b, weight, scale=100.0):
            try:
                if a is None or b is None:
                    return 0.0
                diff = abs((a or 0) - (b or 0))
                return max(0.0, (scale - diff) / scale) * weight
            except Exception:
                return 0.0

        scored = []
        for cand in candidates:
            score = 0.0
            try:
                cand_genre_ids = set(cand.genres.values_list('id', flat=True))
                score += len(set(genre_ids) & cand_genre_ids) * 3.0
            except Exception:
                pass
            try:
                cand_mood_ids = set(cand.moods.values_list('id', flat=True))
                score += len(set(mood_ids) & cand_mood_ids) * 2.0
            except Exception:
                pass
            try:
                cand_tag_ids = set(cand.tags.values_list('id', flat=True))
                score += len(set(tag_ids) & cand_tag_ids) * 1.5
            except Exception:
                pass

            if obj.artist_id and cand.artist_id == obj.artist_id:
                score += 8.0

            score += feature_similarity(obj.energy, cand.energy, 3.0)
            score += feature_similarity(obj.danceability, cand.danceability, 2.5)
            score += feature_similarity(obj.valence, cand.valence, 2.0)
            score += feature_similarity(obj.tempo, cand.tempo, 1.0, scale=200.0)

            try:
                score += min((cand.plays or 0) / 10000.0, 1.0) * 0.5
            except Exception:
                pass

            if score > 0:
                scored.append((cand, score))

        if not scored:
            fallback = Song.objects.filter(status=Song.STATUS_PUBLISHED).exclude(id=obj.id).order_by('-plays')[:50]
            scored = [(s, 0.1) for s in fallback]

        scored.sort(key=lambda x: x[1], reverse=True)
        total = len(scored)
        start = (page - 1) * page_size
        end = start + page_size
        page_items = [item[0] for item in scored[start:end]]

        serializer = SongSummarySerializer(page_items, many=True, context=self.context)
        items_data = serializer.data

        has_next = end < total
        next_link = None
        if has_next and request is not None:
            from urllib.parse import urlencode, urlparse, parse_qs, urlunparse
            parsed = urlparse(request.build_absolute_uri())
            qs = parse_qs(parsed.query)
            qs['similar_page'] = [str(page + 1)]
            qs['similar_page_size'] = [str(page_size)]
            new_query = urlencode(qs, doseq=True)
            next_link = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))

        return {
            'items': items_data,
            'total': total,
            'page': page,
            'has_next': has_next,
            'next': next_link
        }


class SongUploadSerializer(serializers.Serializer):
    """Serializer for uploading songs with audio file"""
    # File uploads
    audio_file = serializers.FileField(required=True, help_text="Audio file (mp3 or wav)")
    cover_image = serializers.ImageField(required=False, allow_null=True, help_text="Cover image")
    
    # Basic info
    title = serializers.CharField(max_length=400, required=True)
    artist_id = serializers.IntegerField(required=True, help_text="Artist ID")
    featured_artists = serializers.ListField(
        child=serializers.CharField(max_length=255),
        required=False,
        allow_empty=True,
        default=list
    )
    album_id = serializers.IntegerField(required=False, allow_null=True)
    is_single = serializers.BooleanField(default=False)
    
    # Metadata
    release_date = serializers.DateField(required=False, allow_null=True)
    language = serializers.CharField(max_length=10, default="fa")
    description = serializers.CharField(required=False, allow_blank=True, default="")
    lyrics = serializers.CharField(required=False, allow_blank=True, default="")
    
    # Classification
    genre_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        allow_empty=True,
        default=list
    )
    sub_genre_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        allow_empty=True,
        default=list
    )
    mood_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        allow_empty=True,
        default=list
    )
    tag_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        allow_empty=True,
        default=list
    )
    
    # Audio features
    tempo = serializers.IntegerField(required=False, allow_null=True)
    energy = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=100)
    danceability = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=100)
    valence = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=100)
    acousticness = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=100)
    instrumentalness = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=100)
    speechiness = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=100)
    live_performed = serializers.BooleanField(default=False)
    
    # Credits
    label = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    producers = serializers.ListField(
        child=serializers.CharField(max_length=255),
        required=False,
        allow_empty=True,
        default=list
    )
    composers = serializers.ListField(
        child=serializers.CharField(max_length=255),
        required=False,
        allow_empty=True,
        default=list
    )
    lyricists = serializers.ListField(
        child=serializers.CharField(max_length=255),
        required=False,
        allow_empty=True,
        default=list
    )
    credits = serializers.CharField(required=False, allow_blank=True, default="")
    
    def validate_audio_file(self, value):
        """Validate audio file format"""
        valid_extensions = ['.mp3', '.wav']
        ext = value.name.lower()[value.name.rfind('.'):]
        if ext not in valid_extensions:
            raise serializers.ValidationError(f"Only {', '.join(valid_extensions)} files are allowed")
        return value
    
    def validate_artist_id(self, value):
        """Validate artist exists"""
        if not Artist.objects.filter(id=value).exists():
            raise serializers.ValidationError("Artist not found")
        return value
    
    def validate_album_id(self, value):
        """Validate album exists if provided"""
        if value and not Album.objects.filter(id=value).exists():
            raise serializers.ValidationError("Album not found")
        return value



class PlaylistSerializer(serializers.ModelSerializer):
    """Serializer for Playlist model"""
    # Read-only nested representations
    genres = GenreSerializer(many=True, read_only=True)
    moods = MoodSerializer(many=True, read_only=True)
    tags = TagSerializer(many=True, read_only=True)
    songs = SongSummarySerializer(many=True, read_only=True)

    # Write fields: accept lists of primary keys
    genre_ids = serializers.PrimaryKeyRelatedField(queryset=Genre.objects.all(), many=True, source='genres', required=False)
    mood_ids = serializers.PrimaryKeyRelatedField(queryset=Mood.objects.all(), many=True, source='moods', required=False)
    tag_ids = serializers.PrimaryKeyRelatedField(queryset=Tag.objects.all(), many=True, source='tags', required=False)
    song_ids = serializers.PrimaryKeyRelatedField(queryset=Song.objects.all(), many=True, source='songs', required=False)

    class Meta:
        model = Playlist
        fields = [
            'id', 'title', 'description', 'cover_image', 'created_at', 'created_by',
            'genres', 'moods', 'tags', 'songs',
            'likes_count', 'is_liked',
            'genre_ids', 'mood_ids', 'tag_ids', 'song_ids'
        ]
        read_only_fields = ['id', 'created_at', 'likes_count', 'is_liked']

    likes_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()

    def get_likes_count(self, obj):
        # Prefer annotated value when available to avoid extra query
        if hasattr(obj, 'likes_count'):
            try:
                return int(obj.likes_count or 0)
            except Exception:
                return 0
        # Fallback to counting M2M relation
        try:
            return obj.liked_by.count()
        except Exception:
            from .models import PlaylistLike
            return PlaylistLike.objects.filter(playlist=obj).count()

    def get_is_liked(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            try:
                # Use prefetched relation when available
                if hasattr(obj, '_prefetched_objects_cache') and 'liked_by' in obj._prefetched_objects_cache:
                    return any(u.id == request.user.id for u in obj.liked_by.all())
                return obj.liked_by.filter(id=request.user.id).exists()
            except Exception:
                from .models import PlaylistLike
                return PlaylistLike.objects.filter(user=request.user, playlist=obj).exists()
        return False


class PlaylistForEventSerializer(serializers.ModelSerializer):
    """Lightweight playlist serializer for EventPlaylist endpoint: use slim genre/mood/tag representation without slug."""
    genres = SlimGenreSerializer(many=True, read_only=True)
    moods = SlimMoodSerializer(many=True, read_only=True)
    tags = SlimTagSerializer(many=True, read_only=True)
    songs = SongSerializer(many=True, read_only=True)

    class Meta:
        model = Playlist
        fields = ['id', 'title', 'description', 'cover_image', 'created_at', 'created_by', 'genres', 'moods', 'tags', 'songs']
        read_only_fields = ['id', 'created_at']


class SongStreamSerializer(serializers.ModelSerializer):
    """Serializer for songs with wrapper stream URLs instead of direct URLs representation"""
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    artist_id = serializers.IntegerField(source='artist.id', read_only=True)
    album_title = serializers.CharField(source='album.title', read_only=True, allow_null=True)
    duration_display = serializers.ReadOnlyField()
    display_title = serializers.ReadOnlyField()
    stream_url = serializers.SerializerMethodField()
    plays = serializers.SerializerMethodField()
    likes_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    
    class Meta:
        model = Song
        fields = [
            'id', 'title', 'artist_id', 'artist_name', 'featured_artists',
            'album', 'album_title', 'is_single', 'stream_url', 'cover_image',
            'duration_seconds', 'duration_display', 'plays', 'likes_count', 'is_liked',
            'status', 'release_date', 'language', 'description',
            'created_at', 'display_title'
        ]
        read_only_fields = ['id', 'plays', 'likes_count', 'is_liked', 'created_at', 'duration_display', 'display_title']
    
    def get_likes_count(self, obj):
        return SongLike.objects.filter(song=obj).count()

    def get_is_liked(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return SongLike.objects.filter(user=request.user, song=obj).exists()
        return False

    def get_stream_url(self, obj):
        """
        CRITICAL: ONLY RETURN UNWRAP LINKS HERE - NEVER DIRECT SIGNED URLS!
        This endpoint MUST ONLY return short wrapper URLs that require unwrapping.
        The actual signed streaming URLs are ONLY returned by the unwrap endpoint.
        DO NOT CHANGE THIS - EVER!
        """
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            # Generate unique short token (8 characters) and avoid collisions
            import secrets
            from uuid import uuid4
            short_token = None
            for _ in range(6):
                candidate = secrets.token_urlsafe(6)[:8]
                if not StreamAccess.objects.filter(short_token=candidate).exists():
                    short_token = candidate
                    break
            if not short_token:
                short_token = uuid4().hex[:8]

            # Generate unique one-time play ID
            unique_otplay_id = None
            for _ in range(6):
                candidate = secrets.token_urlsafe(16)
                if not StreamAccess.objects.filter(unique_otplay_id=candidate).exists():
                    unique_otplay_id = candidate
                    break
            if not unique_otplay_id:
                unique_otplay_id = uuid4().hex

            # Create StreamAccess record
            StreamAccess.objects.create(
                user=request.user,
                song=obj,
                short_token=short_token,
                unique_otplay_id=unique_otplay_id
            )
            
            # Return short URL (UNWRAP LINK ONLY - NOT THE FINAL SIGNED URL!)
            from django.urls import reverse
            short_path = reverse('stream-short', kwargs={'token': short_token})
            
            # Use request to get the origin URL (where the request is coming from)
            request_obj = self.context.get('request')
            if request_obj:
                stream_url = request_obj.build_absolute_uri(short_path)
            else:
                stream_url = short_path
            
            return stream_url
        
        return None

    def get_plays(self, obj):
        # Return the sum of the legacy 'plays' field and the actual PlayCount records
        try:
            return (obj.plays or 0) + obj.play_counts.count()
        except Exception:
            return obj.plays or 0


class UserPlaylistSerializer(serializers.ModelSerializer):
    """Serializer for UserPlaylist model"""
    user_phone = serializers.CharField(source='user.phone_number', read_only=True)
    songs_count = serializers.SerializerMethodField()
    likes_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    top_three_song_covers = serializers.SerializerMethodField()
    type = serializers.ReadOnlyField(default='user-playlist')
    song_ids = serializers.PrimaryKeyRelatedField(
        queryset=Song.objects.all(),
        many=True,
        source='songs',
        required=False
    )
    
    class Meta:
        model = __import__('api.models', fromlist=['UserPlaylist']).UserPlaylist
        fields = [
            'id', 'user', 'user_phone', 'title', 'public', 'songs_count',
            'likes_count', 'is_liked', 'song_ids', 'top_three_song_covers', 
            'type', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user', 'user_phone', 'songs_count', 'likes_count', 
                           'is_liked', 'created_at', 'updated_at', 'top_three_song_covers', 'type']
    
    def get_songs_count(self, obj):
        return obj.songs.count()
    
    def get_likes_count(self, obj):
        return obj.liked_by.count()
    
    def get_is_liked(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return obj.liked_by.filter(id=request.user.id).exists()
        return False

    def get_top_three_song_covers(self, obj):
        try:
            songs = list(obj.songs.all()[:3])
            covers = []
            for s in songs:
                # Direct cover or from album
                cover = s.cover_image or (s.album.cover_image if s.album else None)
                if cover:
                    covers.append(cover)
            return covers
        except Exception:
            return []


class UserPlaylistCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating UserPlaylist with optional first song"""
    first_song_id = serializers.IntegerField(required=False, allow_null=True, write_only=True)
    
    class Meta:
        model = __import__('api.models', fromlist=['UserPlaylist']).UserPlaylist
        fields = ['title', 'public', 'first_song_id']
    
    def create(self, validated_data):
        first_song_id = validated_data.pop('first_song_id', None)
        request = self.context.get('request')
        
        # Create the playlist
        playlist = __import__('api.models', fromlist=['UserPlaylist']).UserPlaylist.objects.create(
            user=request.user,
            **validated_data
        )
        
        # Add the first song if provided
        if first_song_id:
            try:
                song = Song.objects.get(id=first_song_id)
                playlist.songs.add(song)
            except Song.DoesNotExist:
                pass
        
        return playlist

    def get_plays(self, obj):
        try:
            return obj.play_counts.count()
        except Exception:
            return 0


class RecommendedPlaylistListSerializer(serializers.ModelSerializer):
    """Serializer for listing recommended playlists with 3 cover images"""
    covers = serializers.SerializerMethodField()
    songs_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    is_saved = serializers.SerializerMethodField()
    likes_count = serializers.SerializerMethodField()
    genre_names = serializers.SerializerMethodField()
    mood_names = serializers.SerializerMethodField()
    type = serializers.ReadOnlyField(default='recommended')
    
    class Meta:
        model = RecommendedPlaylist
        fields = [
            'id', 'unique_id', 'title', 'description', 'playlist_type',
            'covers', 'songs_count', 'is_liked', 'is_saved', 'likes_count',
            'views', 'relevance_score', 'match_percentage', 'created_at',
            'genre_names', 'mood_names', 'type'
        ]
        read_only_fields = fields

    def get_genre_names(self, obj):
        """Aggregate unique genre names from all songs in this playlist."""
        try:
            names = set()
            for song in obj.songs.prefetch_related('genres').all():
                for g in song.genres.all():
                    names.add(g.name)
            return list(names)
        except Exception:
            return []

    def get_mood_names(self, obj):
        """Aggregate unique mood names from all songs in this playlist."""
        try:
            names = set()
            for song in obj.songs.prefetch_related('moods').all():
                for m in song.moods.all():
                    names.add(m.name)
            return list(names)
        except Exception:
            return []
    
    def get_covers(self, obj):
        """Return first 3 song cover images, respecting explicit `song_order` when present."""
        # If song_order is available, use it to select ordered covers
        try:
            order = obj.song_order if hasattr(obj, 'song_order') and obj.song_order else None
        except Exception:
            order = None

        if order:
            ids = order[:3]
            all_songs = list(obj.songs.all())
            song_map = {s.id: s for s in all_songs if s.id in ids}
            covers = []
            for sid in ids:
                s = song_map.get(sid)
                if s and s.cover_image:
                    covers.append(s.cover_image)
            return covers

        songs = list(obj.songs.all())[:3]
        return [song.cover_image for song in songs if song.cover_image]
    
    def get_songs_count(self, obj):
        return len(obj.songs.all())
    
    def get_is_liked(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return request.user in obj.liked_by.all()
        return False
    
    def get_is_saved(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return request.user in obj.saved_by.all()
        return False
    
    def get_likes_count(self, obj):
        return obj.liked_by.count()


class RecommendedPlaylistDetailSerializer(serializers.ModelSerializer):
    """Serializer for detailed view of recommended playlist with all songs"""
    songs = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    is_saved = serializers.SerializerMethodField()
    likes_count = serializers.SerializerMethodField()
    songs_count = serializers.SerializerMethodField()
    playlist_ref = PlaylistSerializer(read_only=True)
    genre_names = serializers.SerializerMethodField()
    mood_names = serializers.SerializerMethodField()
    
    class Meta:
        model = RecommendedPlaylist
        fields = [
            'id', 'unique_id', 'title', 'description', 'playlist_type',
            'songs', 'songs_count', 'is_liked', 'is_saved', 'likes_count',
            'views', 'relevance_score', 'match_percentage', 'created_at', 'updated_at',
            'playlist_ref', 'genre_names', 'mood_names'
        ]
        read_only_fields = fields

    def get_genre_names(self, obj):
        try:
            names = set()
            for song in obj.songs.prefetch_related('genres').all():
                for g in song.genres.all():
                    names.add(g.name)
            return list(names)
        except Exception:
            return []

    def get_mood_names(self, obj):
        try:
            names = set()
            for song in obj.songs.prefetch_related('moods').all():
                for m in song.moods.all():
                    names.add(m.name)
            return list(names)
        except Exception:
            return []
    
    def get_songs(self, obj):
        """Return all songs with stream links"""
        # Prefer explicit ordering stored in `song_order` if available
        order = None
        try:
            order = obj.song_order if hasattr(obj, 'song_order') and obj.song_order else None
        except Exception:
            order = None

        song_qs = obj.songs.all().select_related('artist', 'album')
        song_map = {s.id: s for s in song_qs}

        ordered_songs = []
        if order:
            for sid in order:
                s = song_map.get(sid)
                if s:
                    ordered_songs.append(s)

        # Fallback to DB order for any songs not in song_order
        if not ordered_songs:
            ordered_songs = list(song_qs)
        else:
            # add any remaining songs that were not present in song_order
            remaining = [s for s in song_qs if s.id not in set(order)]
            ordered_songs.extend(remaining)

        # Use SongStreamSerializer to include stream_url
        return SongStreamSerializer(ordered_songs, many=True, context=self.context).data
    
    def get_is_liked(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return obj.liked_by.filter(id=request.user.id).exists()
        return False
    
    def get_is_saved(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return obj.saved_by.filter(id=request.user.id).exists()
        return False
    
    def get_likes_count(self, obj):
        return obj.liked_by.count()
    
    def get_songs_count(self, obj):
        return obj.songs.count()


class SearchResultSerializer(serializers.Serializer):
    """Unified serializer for search results (Song, Artist, Album, Playlist)."""
    id = serializers.IntegerField()
    type = serializers.SerializerMethodField()
    title = serializers.SerializerMethodField()
    subtitle = serializers.SerializerMethodField()
    image = serializers.SerializerMethodField()
    is_following = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    data = serializers.SerializerMethodField()

    def get_type(self, obj):
        if isinstance(obj, Song): return 'song'
        if isinstance(obj, Artist): return 'artist'
        if isinstance(obj, Album): return 'album'
        if isinstance(obj, (Playlist, UserPlaylist)): return 'playlist'
        return 'unknown'

    def get_title(self, obj):
        if hasattr(obj, 'title'): return obj.title
        if hasattr(obj, 'name'): return obj.name
        return ""

    def get_subtitle(self, obj):
        if isinstance(obj, Song): return obj.artist.name if obj.artist else ""
        if isinstance(obj, Album): return obj.artist.name if obj.artist else ""
        if isinstance(obj, Artist): return "Artist"
        if isinstance(obj, (Playlist, UserPlaylist)): 
            if hasattr(obj, 'user'): return f"By {obj.user.phone_number}"
            if hasattr(obj, 'created_by'): return obj.created_by
            return "Playlist"
        return ""

    def get_image(self, obj):
        if hasattr(obj, 'cover_image'): return obj.cover_image
        if hasattr(obj, 'profile_image'): return obj.profile_image
        return ""

    def get_is_following(self, obj):
        if not isinstance(obj, Artist): return None
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            try:
                # Check Follow table: user following artist
                if Follow.objects.filter(follower_user=request.user, followed_artist=obj).exists():
                    return True
                # If the requester has an artist profile, check artist->artist follows
                artist_profile = getattr(request.user, 'artist_profile', None)
                if artist_profile and Follow.objects.filter(follower_artist=artist_profile, followed_artist=obj).exists():
                    return True
                return False
            except Exception:
                return False
        return False

    def get_is_liked(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return False
        if hasattr(obj, 'liked_by'):
            # Playlist model doesn't have liked_by, but UserPlaylist does
            try:
                return obj.liked_by.filter(id=request.user.id).exists()
            except:
                return False
        return False

    def get_data(self, obj):
        # Return minimal specific data
        if isinstance(obj, Song):
            # Include stream_url for songs in search results
            stream_url = None
            request = self.context.get('request')
            if request and request.user.is_authenticated:
                # We can reuse the logic from SongStreamSerializer or just call it
                # For simplicity and consistency, we'll use the same logic
                from .serializers import SongStreamSerializer
                stream_url = SongStreamSerializer(obj, context=self.context).data.get('stream_url')

            # Calculate total plays (legacy field + PlayCount records)
            total_plays = (obj.plays or 0) + obj.play_counts.count()

            return {
                'duration_seconds': obj.duration_seconds,
                'plays': total_plays,
                'language': obj.language,
                'artist_name': obj.artist.name if obj.artist else None,
                'album_name': obj.album.title if obj.album else None,
                'stream_url': stream_url,
            }
        if isinstance(obj, Artist):
            return {
                'verified': obj.verified,
                'bio': obj.bio[:100] if obj.bio else ""
            }
        if isinstance(obj, Album):
            return {
                'release_date': obj.release_date,
                'artist_name': obj.artist.name if obj.artist else None,
            }
        return {}


class EventPlaylistSerializer(serializers.ModelSerializer):
    """Serializer for EventPlaylist model"""
    # use compact playlist serializer that omits slug fields on nested genres/moods/tags
    playlists = PlaylistForEventSerializer(many=True, read_only=True)

    class Meta:
        model = EventPlaylist
        fields = ['id', 'title', 'time_of_day', 'playlists', 'created_at', 'updated_at']


class PlaylistCoverSerializer(serializers.ModelSerializer):
    """Lightweight playlist serializer used in EventPlaylist list views.
    Uses the first song's cover image as the playlist cover when available.
    """
    cover_image = serializers.SerializerMethodField()

    class Meta:
        model = Playlist
        fields = ['id', 'title', 'description', 'cover_image']
        read_only_fields = fields

    def get_cover_image(self, obj):
        try:
            # prefer an explicit ordering if present
            order = getattr(obj, 'song_order', None)
        except Exception:
            order = None

        try:
            if order:
                first_id = order[0] if len(order) else None
                if first_id:
                    first_song = next((s for s in obj.songs.all() if s.id == first_id), None)
                    if first_song and getattr(first_song, 'cover_image', None):
                        return first_song.cover_image

            first_song = obj.songs.all().first()
            if first_song and getattr(first_song, 'cover_image', None):
                return first_song.cover_image
        except Exception:
            pass

        # fallback to playlist cover field if present
        return getattr(obj, 'cover_image', None)


class EventPlaylistListSerializer(serializers.ModelSerializer):
    """Serializer for listing EventPlaylists with lightweight playlist covers."""
    playlists = PlaylistCoverSerializer(many=True, read_only=True)

    class Meta:
        model = EventPlaylist
        fields = ['id', 'title', 'time_of_day', 'playlists', 'created_at', 'updated_at']
        read_only_fields = fields


class PlaylistDetailForEventSerializer(serializers.ModelSerializer):
    """Playlist serializer for EventPlaylist detail — uses SongSummarySerializer for songs."""
    genres = SlimGenreSerializer(many=True, read_only=True)
    moods = SlimMoodSerializer(many=True, read_only=True)
    tags = SlimTagSerializer(many=True, read_only=True)
    songs = SongSummarySerializer(many=True, read_only=True)

    class Meta:
        model = Playlist
        fields = ['id', 'title', 'description', 'cover_image', 'created_at', 'created_by', 'genres', 'moods', 'tags', 'songs']
        read_only_fields = fields


class EventPlaylistDetailSerializer(serializers.ModelSerializer):
    """Detailed EventPlaylist serializer returning playlists with summarized songs."""
    playlists = PlaylistDetailForEventSerializer(many=True, read_only=True)

    class Meta:
        model = EventPlaylist
        fields = ['id', 'title', 'time_of_day', 'playlists', 'created_at', 'updated_at']
        read_only_fields = fields


class SearchSectionSerializer(serializers.ModelSerializer):
    """Serializer for SearchSection model
    Use `SongSummarySerializer` for song-type sections to keep responses lightweight.
    """
    songs = serializers.SerializerMethodField()
    albums = AlbumSerializer(many=True, read_only=True)
    playlists = PlaylistSerializer(many=True, read_only=True)
    
    song_ids = serializers.PrimaryKeyRelatedField(
        queryset=Song.objects.all(), many=True, write_only=True, source='songs', required=False
    )
    album_ids = serializers.PrimaryKeyRelatedField(
        queryset=Album.objects.all(), many=True, write_only=True, source='albums', required=False
    )
    playlist_ids = serializers.PrimaryKeyRelatedField(
        queryset=Playlist.objects.all(), many=True, write_only=True, source='playlists', required=False
    )

    created_by_name = serializers.ReadOnlyField(source='created_by.phone_number')
    updated_by_name = serializers.ReadOnlyField(source='updated_by.phone_number')

    class Meta:
        model = SearchSection
        fields = [
            'id', 'type', 'title', 'icon_logo', 'item_size', 
            'songs', 'albums', 'playlists', 
            'song_ids', 'album_ids', 'playlist_ids',
            'created_at', 'updated_at', 'created_by', 'updated_by',
            'created_by_name', 'updated_by_name'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by', 'updated_by']

    def get_songs(self, obj):
        try:
            t = (obj.type or '').lower()
        except Exception:
            t = ''

        # If this section represents songs, use the lightweight SongSummarySerializer
        if 'song' in t:
            return SongSummarySerializer(obj.songs.all(), many=True, context=self.context).data

        # otherwise return full SongSerializer (fallback)
        return SongSerializer(obj.songs.all(), many=True, context=self.context).data


class SessionSerializer(serializers.ModelSerializer):
    is_current = serializers.SerializerMethodField()

    class Meta:
        model = RefreshToken
        fields = ['id', 'ip', 'user_agent', 'device_name', 'device_type', 'os_info', 'created_at', 'is_current']

    def get_is_current(self, obj):
        current_token = self.context.get('current_token')
        if not current_token:
            return False
        from django.contrib.auth.hashers import check_password
        return check_password(current_token, obj.token_hash)


class LikedSongSerializer(serializers.ModelSerializer):
    when_liked = serializers.SerializerMethodField()
    
    class Meta:
        model = SongLike
        fields = ['id', 'song', 'when_liked']
    
    def get_when_liked(self, obj):
        from django.utils import timezone
        delta = timezone.now() - obj.created_at
        days = delta.days
        if days == 0:
            return "Today"
        return f"{days} days ago"

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        # Nest the song data
        song_data = SongStreamSerializer(instance.song, context=self.context).data
        ret.update(song_data)
        # Remove the 'song' ID field to avoid confusion
        ret.pop('song', None)
        return ret


class LikedAlbumSerializer(serializers.ModelSerializer):
    when_liked = serializers.SerializerMethodField()
    
    class Meta:
        model = AlbumLike
        fields = ['id', 'album', 'when_liked']
    
    def get_when_liked(self, obj):
        from django.utils import timezone
        delta = timezone.now() - obj.created_at
        days = delta.days
        if days == 0:
            return "Today"
        return f"{days} days ago"

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        # Nest the album data
        album_data = AlbumSerializer(instance.album, context=self.context).data
        ret.update(album_data)
        # Remove the 'album' ID field
        ret.pop('album', None)
        return ret


class LikedPlaylistSerializer(serializers.ModelSerializer):
    when_liked = serializers.SerializerMethodField()
    
    class Meta:
        model = PlaylistLike
        fields = ['id', 'playlist', 'when_liked']
    
    def get_when_liked(self, obj):
        from django.utils import timezone
        delta = timezone.now() - obj.created_at
        days = delta.days
        if days == 0:
            return "Today"
        return f"{days} days ago"

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        # Nest the playlist data
        playlist_data = PlaylistSerializer(instance.playlist, context=self.context).data
        ret.update(playlist_data)
        # Remove the 'playlist' ID field
        ret.pop('playlist', None)
        return ret


class RulesSerializer(serializers.ModelSerializer):
    class Meta:
        model = Rules
        fields = ['id', 'title', 'content', 'version', 'created_at']
        read_only_fields = ['version', 'created_at']

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        # Ensure `updated_at` is never returned in API responses
        ret.pop('updated_at', None)
        return ret


class DepositRequestSerializer(serializers.ModelSerializer):
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    artist_id = serializers.IntegerField(source='artist.id', read_only=True)

    class Meta:
        model = DepositRequest
        fields = [
            'id', 'artist_id', 'artist_name', 'amount', 'status', 
            'transaction_id', 'submission_date', 'status_change_date', 'summary'
        ]
        read_only_fields = ['id', 'artist_id', 'status', 'submission_date', 'status_change_date', 'summary']


class ReportSerializer(serializers.ModelSerializer):
    artist_id = serializers.IntegerField(source='artist.id', required=False, allow_null=True)

    class Meta:
        model = Report
        fields = ['id', 'song', 'artist_id', 'text', 'created_at']
        read_only_fields = ['id', 'created_at']

    def validate(self, data):
        if not data.get('song') and not data.get('artist'):
            raise serializers.ValidationError("Either song or artist must be provided.")
        if data.get('song') and data.get('artist'):
            raise serializers.ValidationError("Only one of song or artist should be provided.")
        return data


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ['id', 'text', 'has_read', 'created_at']
        read_only_fields = ['id', 'created_at']


class AudioAdSerializer(serializers.ModelSerializer):
    class Meta:
        model = AudioAd
        fields = ['id', 'title', 'audio_url', 'image_cover', 'navigate_link', 'duration', 'skippable_after']


