from rest_framework import serializers
from .models import (
    User, Artist, ArtistAuth, NotificationSetting, Song, Album, Genre, SubGenre, 
    Mood, Tag, Report, PlayConfiguration, BannerAd, AudioAd, PaymentTransaction, 
    DepositRequest, SearchSection, EventPlaylist, Playlist
)
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

class AdminEmployeeSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = User
        fields = [
            'id', 'phone_number', 'first_name', 'last_name', 'email',
            'roles', 'is_active', 'is_verified', 'date_joined',
            'permissions', 'password'
        ]
        read_only_fields = ['id', 'date_joined']

    def create(self, validated_data):
        password = validated_data.pop('password', None)
        user = super().create(validated_data)
        if password:
            user.set_password(password)
            user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        user = super().update(instance, validated_data)
        if password:
            user.set_password(password)
            user.save()
        return user

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


class AdminAlbumSerializer(serializers.ModelSerializer):
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    songs = AdminSongSerializer(many=True, read_only=True)
    cover_image_upload = serializers.ImageField(write_only=True, required=False)
    
    # For write operations
    genres = serializers.PrimaryKeyRelatedField(queryset=Genre.objects.all(), many=True, required=False)
    sub_genres = serializers.PrimaryKeyRelatedField(queryset=SubGenre.objects.all(), many=True, required=False)
    moods = serializers.PrimaryKeyRelatedField(queryset=Mood.objects.all(), many=True, required=False)

    class Meta:
        model = Album
        fields = [
            'id', 'title', 'artist', 'artist_name', 'cover_image', 'cover_image_upload',
            'release_date', 'description', 'genres', 'sub_genres', 'moods',
            'created_at', 'songs'
        ]
        read_only_fields = ['id', 'cover_image', 'created_at']


class AdminPlayConfigurationSerializer(serializers.ModelSerializer):
    per_normal_play_pay = serializers.DecimalField(source='free_play_worth', max_digits=12, decimal_places=8)
    per_premium_play_pay = serializers.DecimalField(source='premium_play_worth', max_digits=12, decimal_places=8)

    class Meta:
        model = PlayConfiguration
        fields = [
            'premium_plan_price', 'per_normal_play_pay', 'per_premium_play_pay', 'ad_frequency', 'updated_at'
        ]
        read_only_fields = ['updated_at']


class AdminBannerAdSerializer(serializers.ModelSerializer):
    image_upload = serializers.ImageField(write_only=True, required=False)

    class Meta:
        model = BannerAd
        fields = ['id', 'title', 'image', 'image_upload', 'navigate_link', 'is_active', 'created_at', 'updated_at']
        read_only_fields = ['id', 'image', 'created_at', 'updated_at']


class AdminAudioAdSerializer(serializers.ModelSerializer):
    audio_upload = serializers.FileField(write_only=True, required=False)
    image_cover_upload = serializers.ImageField(write_only=True, required=False)

    class Meta:
        model = AudioAd
        fields = [
            'id', 'title', 'audio_url', 'audio_upload', 'image_cover', 'image_cover_upload',
            'navigate_link', 'duration', 'skippable_after', 'is_active', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'audio_url', 'image_cover', 'created_at', 'updated_at']


class AdminPaymentTransactionSerializer(serializers.ModelSerializer):
    user_phone = serializers.CharField(source='user.phone_number', read_only=True)

    class Meta:
        model = PaymentTransaction
        fields = ['id', 'user', 'user_phone', 'transaction_id', 'amount', 'status', 'payment_method', 'description', 'created_at']
        read_only_fields = ['id', 'created_at']


class AdminDepositRequestSerializer(serializers.ModelSerializer):
    artist_name = serializers.CharField(source='artist.name', read_only=True)

    class Meta:
        model = DepositRequest
        fields = ['id', 'artist', 'artist_name', 'amount', 'status', 'transaction_id', 'submission_date', 'status_change_date', 'summary']
        read_only_fields = ['id', 'submission_date', 'status_change_date']


class AdminPlaylistSerializer(serializers.ModelSerializer):
    cover_image_upload = serializers.ImageField(write_only=True, required=False)
    
    class Meta:
        model = Playlist
        fields = ['id', 'title', 'description', 'cover_image', 'cover_image_upload', 'created_by', 'created_at']
        read_only_fields = ['id', 'cover_image', 'created_at']


class AdminSearchSectionSerializer(serializers.ModelSerializer):
    icon_logo_upload = serializers.ImageField(write_only=True, required=False)
    
    class Meta:
        model = SearchSection
        fields = [
            'id', 'type', 'title', 'icon_logo', 'icon_logo_upload', 'item_size',
            'songs', 'albums', 'playlists', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'icon_logo', 'created_at', 'updated_at']


class AdminEventPlaylistSerializer(serializers.ModelSerializer):
    cover_image_upload = serializers.ImageField(write_only=True, required=False)
    
    class Meta:
        model = EventPlaylist
        fields = ['id', 'title', 'time_of_day', 'cover_image', 'cover_image_upload', 'playlists', 'created_at', 'updated_at']
        read_only_fields = ['id', 'cover_image', 'created_at', 'updated_at']

