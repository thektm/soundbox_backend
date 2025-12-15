from rest_framework import serializers
from .models import User, Artist, Album, Genre, Mood, Tag, SubGenre, Song
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


class AlbumSerializer(serializers.ModelSerializer):
    """Serializer for Album model"""
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    
    class Meta:
        model = Album
        fields = ['id', 'title', 'artist', 'artist_name', 'cover_image', 'release_date', 'description', 'created_at']
        read_only_fields = ['id', 'created_at']


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
            'album', 'album_title', 'is_single', 'audio_file', 'cover_image',
            'original_format', 'duration_seconds', 'duration_display', 'plays',
            'status', 'release_date', 'language', 'genre_ids', 'sub_genre_ids',
            'mood_ids', 'tag_ids', 'description', 'lyrics', 'tempo', 'energy',
            'danceability', 'valence', 'acousticness', 'instrumentalness',
            'live_performed', 'speechiness', 'label', 'producers', 'composers',
            'lyricists', 'credits', 'uploader', 'uploader_phone', 'created_at',
            'updated_at', 'display_title'
        ]
        read_only_fields = ['id', 'plays', 'created_at', 'updated_at', 'duration_display', 'display_title']


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
