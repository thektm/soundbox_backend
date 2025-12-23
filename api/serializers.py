from rest_framework import serializers
from .models import User,UserPlaylist, Artist, Album, Genre, Mood, Tag, SubGenre, Song, Playlist, StreamAccess, RecommendedPlaylist
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer


class UserSerializer(serializers.ModelSerializer):
    followers = serializers.SerializerMethodField()
    followings = serializers.PrimaryKeyRelatedField(queryset=get_user_model().objects.all(), many=True, required=False)
    playlists = serializers.JSONField()
    settings = serializers.JSONField()
    class Meta:
        model = get_user_model()
        fields = [
            'id', 'phone_number', 'first_name', 'last_name', 'email',
            'roles', 'is_active', 'is_staff', 'date_joined',
            'followers', 'followings', 'playlists', 'plan', 'settings'
        ]
        read_only_fields = ['id', 'is_active', 'is_staff', 'date_joined', 'followers']

    def get_followers(self, obj):
        # return minimal follower info: id and phone_number
        return [{'id': u.id, 'phone_number': u.phone_number} for u in obj.followers.all()]


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    playlists = serializers.JSONField(required=False)
    settings = serializers.JSONField(required=False)

    class Meta:
        model = User
        fields = ['phone_number', 'password', 'roles', 'first_name', 'last_name', 'email', 'playlists', 'plan', 'settings']

    def validate_phone_number(self, value):
        if User.objects.filter(phone_number=value).exists():
            raise serializers.ValidationError('A user with that phone number already exists')
        return value

    def create(self, validated_data):
        password = validated_data.pop('password')
        user = User.objects.create_user(password=password, **validated_data)
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


class VerifySerializer(serializers.Serializer):
    phone = serializers.CharField()
    otp = serializers.CharField()
    context = serializers.CharField(required=False, allow_blank=True)


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


class LogoutSerializer(serializers.Serializer):
    refreshToken = serializers.CharField()


class ArtistSerializer(serializers.ModelSerializer):
    """Serializer for Artist model"""
    user_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), 
        source='user', 
        required=False, 
        allow_null=True
    )
    
    class Meta:
        model = Artist
        fields = ['id', 'name', 'user_id', 'bio', 'profile_image', 'verified', 'created_at']
        read_only_fields = ['id', 'created_at']


class PopularArtistSerializer(ArtistSerializer):
    """Artist serializer extended with popularity metrics."""
    total_plays = serializers.IntegerField(read_only=True)
    total_likes = serializers.IntegerField(read_only=True)
    total_playlist_adds = serializers.IntegerField(read_only=True)
    weekly_plays = serializers.IntegerField(read_only=True)
    daily_plays = serializers.IntegerField(read_only=True)
    score = serializers.IntegerField(read_only=True)

    class Meta(ArtistSerializer.Meta):
        fields = ArtistSerializer.Meta.fields + [
            'total_plays', 'total_likes', 'total_playlist_adds', 'weekly_plays', 'daily_plays', 'score'
        ]


class AlbumSerializer(serializers.ModelSerializer):
    """Serializer for Album model"""
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    
    class Meta:
        model = Album
        fields = ['id', 'title', 'artist', 'artist_name', 'cover_image', 'release_date', 'description', 'created_at']
        read_only_fields = ['id', 'created_at']


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
        # obj.songs may be prefetched by the view; choose first 3 by release_date then created_at
        try:
            songs_qs = obj.songs.all()
            # If songs were not prefetched in the view, this will hit DB once per album
            top = songs_qs.order_by('-release_date', '-created_at')[:3]
            return [s.cover_image for s in top if s.cover_image]
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


class SongSerializer(serializers.ModelSerializer):
    """Serializer for Song model with full details"""
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    album_title = serializers.CharField(source='album.title', read_only=True, allow_null=True)
    uploader_phone = serializers.CharField(source='uploader.phone_number', read_only=True, allow_null=True)
    duration_display = serializers.ReadOnlyField()
    display_title = serializers.ReadOnlyField()
    stream_url = serializers.SerializerMethodField()
    plays = serializers.SerializerMethodField()
    likes_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    
    # For write operations
    genre_ids = serializers.PrimaryKeyRelatedField(
        queryset=Genre.objects.all(), 
        many=True, 
        source='genres', 
        required=False
    )
    sub_genre_ids = serializers.PrimaryKeyRelatedField(
        queryset=SubGenre.objects.all(), 
        many=True, 
        source='sub_genres', 
        required=False
    )
    mood_ids = serializers.PrimaryKeyRelatedField(
        queryset=Mood.objects.all(), 
        many=True, 
        source='moods', 
        required=False
    )
    tag_ids = serializers.PrimaryKeyRelatedField(
        queryset=Tag.objects.all(), 
        many=True, 
        source='tags', 
        required=False
    )
    
    class Meta:
        model = Song
        fields = [
            'id', 'title', 'artist', 'artist_name', 'featured_artists',
            'album', 'album_title', 'is_single', 'stream_url', 'cover_image',
            'original_format', 'duration_seconds', 'duration_display', 'plays',
            'likes_count', 'is_liked',
            'status', 'release_date', 'language', 'genre_ids', 'sub_genre_ids',
            'mood_ids', 'tag_ids', 'description', 'lyrics', 'tempo', 'energy',
            'danceability', 'valence', 'acousticness', 'instrumentalness',
            'live_performed', 'speechiness', 'label', 'producers', 'composers',
            'lyricists', 'credits', 'uploader', 'uploader_phone', 'created_at',
            'updated_at', 'display_title'
        ]
        read_only_fields = ['id', 'plays', 'likes_count', 'is_liked', 'created_at', 'updated_at', 'duration_display', 'display_title']

    def get_plays(self, obj):
        try:
            return obj.play_counts.count()
        except Exception:
            return 0

    def get_likes_count(self, obj):
        return obj.liked_by.count()

    def get_is_liked(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return obj.liked_by.filter(id=request.user.id).exists()
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
    songs = SongSerializer(many=True, read_only=True)

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
            'genre_ids', 'mood_ids', 'tag_ids', 'song_ids'
        ]
        read_only_fields = ['id', 'created_at']


class SongStreamSerializer(serializers.ModelSerializer):
    """Serializer for songs with wrapper stream URLs instead of direct URLs"""
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    album_title = serializers.CharField(source='album.title', read_only=True, allow_null=True)
    duration_display = serializers.ReadOnlyField()
    display_title = serializers.ReadOnlyField()
    stream_url = serializers.SerializerMethodField()
    plays = serializers.SerializerMethodField()
    
    class Meta:
        model = Song
        fields = [
            'id', 'title', 'artist', 'artist_name', 'featured_artists',
            'album', 'album_title', 'is_single', 'stream_url', 'cover_image',
            'duration_seconds', 'duration_display', 'plays',
            'status', 'release_date', 'language', 'description',
            'created_at', 'display_title'
        ]
        read_only_fields = ['id', 'plays', 'created_at', 'duration_display', 'display_title']
    
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
        try:
            return obj.play_counts.count()
        except Exception:
            return 0


class UserPlaylistSerializer(serializers.ModelSerializer):
    """Serializer for UserPlaylist model"""
    user_phone = serializers.CharField(source='user.phone_number', read_only=True)
    songs_count = serializers.SerializerMethodField()
    likes_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
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
            'likes_count', 'is_liked', 'song_ids', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user', 'user_phone', 'songs_count', 'likes_count', 
                           'is_liked', 'created_at', 'updated_at']
    
    def get_songs_count(self, obj):
        return obj.songs.count()
    
    def get_likes_count(self, obj):
        return obj.liked_by.count()
    
    def get_is_liked(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return obj.liked_by.filter(id=request.user.id).exists()
        return False


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
    
    class Meta:
        model = RecommendedPlaylist
        fields = [
            'id', 'unique_id', 'title', 'description', 'playlist_type',
            'covers', 'songs_count', 'is_liked', 'is_saved', 'likes_count',
            'views', 'relevance_score', 'match_percentage', 'created_at'
        ]
        read_only_fields = fields
    
    def get_covers(self, obj):
        """Return first 3 song cover images, respecting explicit `song_order` when present."""
        # If song_order is available, use it to select ordered covers
        try:
            order = obj.song_order if hasattr(obj, 'song_order') and obj.song_order else None
        except Exception:
            order = None

        if order:
            ids = order[:3]
            song_map = {s.id: s for s in obj.songs.filter(id__in=ids)}
            covers = []
            for sid in ids:
                s = song_map.get(sid)
                if s and s.cover_image:
                    covers.append(s.cover_image)
            return covers

        songs = obj.songs.all()[:3]
        return [song.cover_image for song in songs if song.cover_image]
    
    def get_songs_count(self, obj):
        return obj.songs.count()
    
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


class RecommendedPlaylistDetailSerializer(serializers.ModelSerializer):
    """Serializer for detailed view of recommended playlist with all songs"""
    songs = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    is_saved = serializers.SerializerMethodField()
    likes_count = serializers.SerializerMethodField()
    songs_count = serializers.SerializerMethodField()
    
    class Meta:
        model = RecommendedPlaylist
        fields = [
            'id', 'unique_id', 'title', 'description', 'playlist_type',
            'songs', 'songs_count', 'is_liked', 'is_saved', 'likes_count',
            'views', 'relevance_score', 'match_percentage', 'created_at', 'updated_at'
        ]
        read_only_fields = fields
    
    def get_songs(self, obj):
        """Return all songs without stream links, only cover images"""
        # Prefer explicit ordering stored in `song_order` if available
        order = None
        try:
            order = obj.song_order if hasattr(obj, 'song_order') and obj.song_order else None
        except Exception:
            order = None

        song_qs = obj.songs.all()
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

        song_data = []
        for song in ordered_songs:
            data = {
                'id': song.id,
                'title': song.title,
                'display_title': song.display_title,
                'artist': {
                    'id': song.artist.id,
                    'name': song.artist.name,
                    'profile_image': song.artist.profile_image
                } if song.artist else None,
                'album': {
                    'id': song.album.id,
                    'title': song.album.title,
                    'cover_image': song.album.cover_image
                } if song.album else None,
                'cover_image': song.cover_image,
                'duration_seconds': song.duration_seconds,
                'duration_display': song.duration_display,
                'plays': song.plays,
                'release_date': song.release_date,
                'language': song.language,
            }
            song_data.append(data)

        return song_data
    
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
            return request.user.followings.filter(id=obj.id).exists()
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
            return {
                'duration_seconds': obj.duration_seconds,
                'plays': obj.plays,
                'language': obj.language,
                'artist_name': obj.artist.name if obj.artist else None,
                'album_name': obj.album.title if obj.album else None,
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
