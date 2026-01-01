from rest_framework import serializers
from .models import User, Artist, ArtistAuth, NotificationSetting, Song, Album, Genre, SubGenre, Mood, Tag, Report
from django.contrib.auth import get_user_model

User = get_user_model()

class AdminUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            'id', 'phone_number', 'first_name', 'last_name', 'email',
            'roles', 'is_active', 'is_staff', 'is_verified', 'date_joined',
            'plan', 'stream_quality', 'last_login_at', 'failed_login_attempts', 'locked_until'
        ]
        read_only_fields = ['id', 'date_joined', 'last_login_at']

class AdminArtistSerializer(serializers.ModelSerializer):
    has_user = serializers.SerializerMethodField()
    user_phone = serializers.CharField(source='user.phone_number', read_only=True)

    class Meta:
        model = Artist
        fields = [
            'id', 'name', 'artistic_name', 'email', 'city', 'date_of_birth',
            'address', 'id_number', 'user', 'user_phone', 'bio', 'profile_image',
            'banner_image', 'verified', 'created_at', 'has_user'
        ]
        read_only_fields = ['id', 'created_at']

    def get_has_user(self, obj):
        return obj.user is not None

class AdminArtistAuthSerializer(serializers.ModelSerializer):
    class Meta:
        model = ArtistAuth
        fields = [
            'id', 'user', 'auth_type', 'artist_claimed', 'first_name', 'last_name', 'stage_name',
            'birth_date', 'national_id', 'phone_number', 'email', 'city', 'address',
            'biography', 'national_id_image', 'status', 'is_verified', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

class AdminSongSerializer(serializers.ModelSerializer):
    # We use FileField for uploads, but the model stores URLField
    audio_file_upload = serializers.FileField(write_only=True, required=False)
    cover_image_upload = serializers.ImageField(write_only=True, required=False)
    
    # Display fields
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    album_title = serializers.CharField(source='album.title', read_only=True, allow_null=True)

    # JSON fields as ListFields for better form-data handling
    featured_artists = serializers.ListField(child=serializers.CharField(), required=False)
    producers = serializers.ListField(child=serializers.CharField(), required=False)
    composers = serializers.ListField(child=serializers.CharField(), required=False)
    lyricists = serializers.ListField(child=serializers.CharField(), required=False)

    class Meta:
        model = Song
        fields = [
            'id', 'title', 'artist', 'artist_name', 'featured_artists', 'album', 'album_title',
            'is_single', 'audio_file', 'converted_audio_url', 'cover_image', 'original_format',
            'duration_seconds', 'plays', 'status', 'release_date', 'language',
            'genres', 'sub_genres', 'moods', 'tags', 'description', 'lyrics',
            'tempo', 'energy', 'danceability', 'valence', 'acousticness',
            'instrumentalness', 'live_performed', 'speechiness', 'label',
            'producers', 'composers', 'lyricists', 'credits', 'uploader',
            'created_at', 'updated_at', 'audio_file_upload', 'cover_image_upload'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'plays']


class AdminReportSerializer(serializers.ModelSerializer):
    user_phone = serializers.CharField(source='user.phone_number', read_only=True)
    song_title = serializers.CharField(source='song.title', read_only=True, allow_null=True)
    artist_name = serializers.CharField(source='artist.name', read_only=True, allow_null=True)

    class Meta:
        model = Report
        fields = [
            'id', 'user', 'user_phone', 'song', 'song_title', 'artist', 'artist_name',
            'text', 'has_reviewed', 'reviewed_at', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user', 'song', 'artist', 'created_at', 'updated_at']
