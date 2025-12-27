from django.db import models
from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.core.validators import MinValueValidator, MaxValueValidator
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, RegexValidator
import os


class UserManager(BaseUserManager):
    def create_user(self, phone_number, password=None, roles='audience', **extra_fields):
        if not phone_number:
            raise ValueError('The phone number must be set')
        phone_number = str(phone_number)
        # allow caller to pass artist_password in extra_fields
        artist_password = extra_fields.pop('artist_password', None)
        user = self.model(phone_number=phone_number, roles=roles, **extra_fields)
        user.set_password(password)
        if artist_password:
            user.set_artist_password(artist_password)
        user.save(using=self._db)
        return user

    def create_superuser(self, phone_number, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(phone_number, password=password, roles='admin', **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    ROLE_AUDIENCE = 'audience'
    ROLE_ARTIST = 'artist'
    ROLE_ADMIN = 'admin'

    ROLE_CHOICES = [
        (ROLE_AUDIENCE, 'Audience'),
        (ROLE_ARTIST, 'Artist'),
        (ROLE_ADMIN, 'Admin'),
    ]

    phone_number = models.CharField(max_length=20, unique=True)
    # Phone verification flag
    is_verified = models.BooleanField(default=False)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    email = models.EmailField(blank=True, null=True)
    roles = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_AUDIENCE)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True)
    # Additional auth/audit fields
    last_login_at = models.DateTimeField(null=True, blank=True)
    failed_login_attempts = models.IntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    # playlists: store as JSON (list of playlist objects or ids); can be replaced with real Playlist model later
    playlists = models.JSONField(default=list, blank=True)

    # current plan for user
    PLAN_FREE = 'free'
    PLAN_PREMIUM = 'premium'
    PLAN_CHOICES = [
        (PLAN_FREE, 'Free'),
        (PLAN_PREMIUM, 'Premium'),
    ]
    plan = models.CharField(max_length=30, choices=PLAN_CHOICES, default=PLAN_FREE)

    # Stream quality settings
    QUALITY_MEDIUM = 'medium'
    QUALITY_HIGH = 'high'
    QUALITY_CHOICES = [
        (QUALITY_MEDIUM, 'Medium'),
        (QUALITY_HIGH, 'High'),
    ]
    stream_quality = models.CharField(max_length=20, choices=QUALITY_CHOICES, default=QUALITY_MEDIUM)

    # user-specific settings stored as JSON
    settings = models.JSONField(default=dict, blank=True)
    # Optional separate password for artist dashboard / artist-specific login
    artist_password = models.CharField(max_length=255, blank=True, null=True)

    objects = UserManager()

    USERNAME_FIELD = 'phone_number'
    REQUIRED_FIELDS = []

    def __str__(self):
        return self.phone_number

    def set_artist_password(self, raw_password):
        from django.contrib.auth.hashers import make_password
        if raw_password:
            self.artist_password = make_password(raw_password)
        else:
            self.artist_password = None

    def check_artist_password(self, raw_password):
        from django.contrib.auth.hashers import check_password
        if not self.artist_password:
            return False
        return check_password(raw_password, self.artist_password)

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            NotificationSetting.objects.get_or_create(user=self)


class Follow(models.Model):
    """Unified follow model for User-User, User-Artist, Artist-User, Artist-Artist"""
    # Follower
    follower_user = models.ForeignKey('User', on_delete=models.CASCADE, null=True, blank=True, related_name='following_user_relations')
    follower_artist = models.ForeignKey('Artist', on_delete=models.CASCADE, null=True, blank=True, related_name='following_artist_relations')
    
    # Followed
    followed_user = models.ForeignKey('User', on_delete=models.CASCADE, null=True, blank=True, related_name='follower_user_relations')
    followed_artist = models.ForeignKey('Artist', on_delete=models.CASCADE, null=True, blank=True, related_name='follower_artist_relations')
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['follower_user', 'followed_user'], 
                name='unique_user_follow_user', 
                condition=models.Q(follower_user__isnull=False, followed_user__isnull=False)
            ),
            models.UniqueConstraint(
                fields=['follower_user', 'followed_artist'], 
                name='unique_user_follow_artist', 
                condition=models.Q(follower_user__isnull=False, followed_artist__isnull=False)
            ),
            models.UniqueConstraint(
                fields=['follower_artist', 'followed_user'], 
                name='unique_artist_follow_user', 
                condition=models.Q(follower_artist__isnull=False, followed_user__isnull=False)
            ),
            models.UniqueConstraint(
                fields=['follower_artist', 'followed_artist'], 
                name='unique_artist_follow_artist', 
                condition=models.Q(follower_artist__isnull=False, followed_artist__isnull=False)
            ),
        ]

    def __str__(self):
        follower = self.follower_user or self.follower_artist
        followed = self.followed_user or self.followed_artist
        return f"{follower} follows {followed}"


class NotificationSetting(models.Model):
    """User notification preferences"""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='notification_setting')
    
    # 1. New song from followed artists (Default: ON)
    new_song_followed_artists = models.BooleanField(default=True)
    
    # 2. New album from followed artists (Default: ON)
    new_album_followed_artists = models.BooleanField(default=True)
    
    # 3. New playlist (Default: OFF)
    new_playlist = models.BooleanField(default=False)
    
    # 4. New likes (Default: ON) - (Comments removed as per request)
    new_likes = models.BooleanField(default=True)
    
    # 5. New follower (Default: ON)
    new_follower = models.BooleanField(default=True)
    
    # 6. System notifications / App updates (Default: ON)
    system_notifications = models.BooleanField(default=True)

    def __str__(self):
        return f"Notification settings for {self.user.phone_number}"


class OtpCode(models.Model):
    PURPOSE_VERIFY = 'verify_account'
    PURPOSE_LOGIN = 'login'
    PURPOSE_RESET = 'reset_password'

    PURPOSE_CHOICES = [
        (PURPOSE_VERIFY, 'Verify Account'),
        (PURPOSE_LOGIN, 'Login'),
        (PURPOSE_RESET, 'Reset Password'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='otp_codes')
    code_hash = models.CharField(max_length=255)
    purpose = models.CharField(max_length=50, choices=PURPOSE_CHOICES)
    expires_at = models.DateTimeField()
    attempts = models.IntegerField(default=0)
    consumed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['purpose', 'created_at']), models.Index(fields=['expires_at'])]

    def __str__(self):
        return f"OTP({self.purpose}) for user={self.user_id} consumed={self.consumed}"


class RefreshToken(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='refresh_tokens')
    token_hash = models.CharField(max_length=255)
    user_agent = models.CharField(max_length=512, blank=True)
    ip = models.CharField(max_length=64, blank=True)
    device_name = models.CharField(max_length=255, blank=True, null=True)
    device_type = models.CharField(max_length=100, blank=True, null=True)
    os_info = models.CharField(max_length=100, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"RefreshToken(user={self.user_id} device={self.device_name} expires={self.expires_at} revoked={self.revoked_at is not None})"


class Artist(models.Model):
    """Artist model - can be linked to a user account or standalone"""
    name = models.CharField(max_length=255, unique=True)
    artistic_name = models.CharField(max_length=255, blank=True, help_text="Artistic/stage name")
    email = models.EmailField(blank=True, null=True)
    city = models.CharField(max_length=200, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    address = models.TextField(blank=True)
    id_number = models.CharField(max_length=100, blank=True)
    user = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='artist_profile')
    bio = models.TextField(blank=True)
    profile_image = models.URLField(max_length=500, blank=True, help_text="R2 CDN URL for profile image")
    banner_image = models.URLField(max_length=500, blank=True, help_text="R2 CDN URL for banner image")
    verified = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['name']
    
    def __str__(self):
        return self.name


def validate_file_size(value):
    limit = 5 * 1024 * 1024  # 5MB
    try:
        size = value.size
    except Exception:
        # If value doesn't have size (unlikely in normal ImageField usage), skip
        return
    if size > limit:
        raise ValidationError('Max file size is 5MB')


class ArtistAuth(models.Model):
    """Artist authentication / verification submissions.

    Captures identity details and uploaded national ID image used for
    verifying an artist (existing or fresh).
    """
    AUTH_EXISTING = 'existing_artist'
    AUTH_FRESH = 'fresh_artist'

    AUTH_CHOICES = [
        (AUTH_EXISTING, 'Existing Artist'),
        (AUTH_FRESH, 'Fresh Artist'),
    ]

    user = models.OneToOneField('User', on_delete=models.CASCADE, null=True, blank=True, related_name='artist_auth')
    auth_type = models.CharField(max_length=30, choices=AUTH_CHOICES, default=AUTH_FRESH)

    # Personal fields (Persian form mapping provided by frontend)
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    stage_name = models.CharField(max_length=200)
    birth_date = models.DateField()

    national_id_validator = RegexValidator(regex=r'^\d{10}$', message='National ID must be exactly 10 digits')
    national_id = models.CharField(max_length=10, validators=[national_id_validator])

    phone_validator = RegexValidator(regex=r'^09\d{9}$', message='Phone number must be 11 digits and start with 09')
    phone_number = models.CharField(max_length=20, validators=[phone_validator])

    email = models.EmailField(blank=True, null=True)
    city = models.CharField(max_length=100)
    address = models.TextField(blank=True, null=True)
    biography = models.TextField(blank=True, null=True)

    national_id_image = models.ImageField(
        upload_to='national_ids/',
        validators=[FileExtensionValidator(['jpg', 'jpeg', 'png']), validate_file_size]
    )

    # Verification status for the submission
    STATUS_PENDING = 'pending'
    STATUS_ACCEPTED = 'accepted'
    STATUS_REJECTED = 'rejected'

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_ACCEPTED, 'Accepted'),
        (STATUS_REJECTED, 'Rejected'),
    ]

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)

    is_verified = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.stage_name} ({self.user.phone_number if self.user else 'no-user'})"

    def clean(self):
        # Ensure national id has correct format (redundant with validator but safe)
        if self.national_id and (not self.national_id.isdigit() or len(self.national_id) != 10):
            raise ValidationError({'national_id': 'National ID must be exactly 10 digits'})
        # Phone number basic normalization check
        if self.phone_number:
            digits = ''.join(ch for ch in self.phone_number if ch.isdigit())
            if not digits.startswith('09') or len(digits) != 11:
                raise ValidationError({'phone_number': 'Phone number must be in local format starting with 09 and 11 digits'})


class ArtistMonthlyListener(models.Model):
    """Track unique listeners per artist to calculate monthly listeners (last 28 days)"""
    artist = models.ForeignKey(Artist, on_delete=models.CASCADE, related_name='monthly_listener_records')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='artist_listened_to')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('artist', 'user')
        indexes = [
            models.Index(fields=['artist', 'updated_at']),
        ]

    def __str__(self):
        return f"{self.user.phone_number} listened to {self.artist.name} (last: {self.updated_at})"


class Album(models.Model):
    """Album model for grouping songs"""
    title = models.CharField(max_length=400)
    artist = models.ForeignKey(Artist, on_delete=models.CASCADE, related_name="albums")
    cover_image = models.URLField(max_length=500, blank=True, help_text="R2 CDN URL for album cover")
    release_date = models.DateField(null=True, blank=True)
    description = models.TextField(blank=True)
    # Classification: albums can also be tagged with genres, sub-genres and moods
    genres = models.ManyToManyField('Genre', blank=True, related_name="albums")
    sub_genres = models.ManyToManyField('SubGenre', blank=True, related_name="albums")
    moods = models.ManyToManyField('Mood', blank=True, related_name="albums")
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-release_date']
    
    def __str__(self):
        return f"{self.title} - {self.artist.name}"

    # Likes
    liked_by = models.ManyToManyField(User, blank=True, related_name='liked_albums', through='AlbumLike')


class AlbumLike(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    album = models.ForeignKey('Album', on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'album')
        ordering = ['-created_at']


class Genre(models.Model):
    """Music genre classification"""
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    
    class Meta:
        ordering = ['name']
    
    def __str__(self):
        return self.name


class Mood(models.Model):
    """Mood/vibe classification for songs"""
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    
    class Meta:
        ordering = ['name']
    
    def __str__(self):
        return self.name


class Tag(models.Model):
    """Generic tags for songs"""
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    
    class Meta:
        ordering = ['name']
    
    def __str__(self):
        return self.name


class SubGenre(models.Model):
    """Sub-genre classification for songs"""
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    parent_genre = models.ForeignKey(Genre, on_delete=models.CASCADE, related_name='sub_genres', null=True, blank=True)
    
    class Meta:
        ordering = ['name']
    
    def __str__(self):
        return self.name


class Song(models.Model):
    """Main song model with comprehensive metadata"""
    
    # Status choices
    STATUS_DRAFT = 'draft'
    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    STATUS_PUBLISHED = 'published'
    
    STATUS_CHOICES = [
        (STATUS_DRAFT, 'Draft'),
        (STATUS_PENDING, 'Pending Review'),
        (STATUS_APPROVED, 'Approved'),
        (STATUS_REJECTED, 'Rejected'),
        (STATUS_PUBLISHED, 'Published'),
    ]
    
    # Basic info
    title = models.CharField(max_length=400)
    artist = models.ForeignKey(Artist, on_delete=models.CASCADE, related_name="songs")
    featured_artists = models.JSONField(default=list, blank=True, help_text="List of featured artist names")
    album = models.ForeignKey(Album, on_delete=models.SET_NULL, null=True, blank=True, related_name="songs")
    is_single = models.BooleanField(default=False)

    # Files (R2 CDN URLs)
    audio_file = models.URLField(max_length=500, help_text="R2 CDN URL for audio file")
    cover_image = models.URLField(max_length=500, blank=True, help_text="R2 CDN URL for cover image")
    original_format = models.CharField(max_length=10, blank=True, help_text="Original upload format (mp3, wav, etc.)")

    # Playback/display
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    plays = models.BigIntegerField(default=0)

    # Status / Moderation
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)

    # Metadata
    release_date = models.DateField(null=True, blank=True)
    language = models.CharField(max_length=10, default="fa")

    # Classification
    genres = models.ManyToManyField(Genre, blank=True, related_name="songs")
    sub_genres = models.ManyToManyField(SubGenre, blank=True, related_name="songs")
    moods = models.ManyToManyField(Mood, blank=True, related_name="songs")
    tags = models.ManyToManyField(Tag, blank=True, related_name="songs")

    # Description & lyrics
    description = models.TextField(blank=True)
    lyrics = models.TextField(blank=True)

    # Audio features (0-100 or boolean)
    tempo = models.PositiveSmallIntegerField(null=True, blank=True, help_text="BPM")
    energy = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(100)])
    danceability = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(100)])
    valence = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(100)])
    acousticness = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(100)])
    instrumentalness = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(100)])
    live_performed = models.BooleanField(default=False)
    speechiness = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(100)])

    # Legal / credits
    label = models.CharField(max_length=255, blank=True)
    producers = models.JSONField(default=list, blank=True, help_text="List of producer names")
    composers = models.JSONField(default=list, blank=True, help_text="List of composer names")
    lyricists = models.JSONField(default=list, blank=True, help_text="List of lyricist names")
    credits = models.TextField(blank=True)

    # Ownership + audit
    uploader = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="uploaded_songs", help_text="User who uploaded the song")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["release_date"]),
            models.Index(fields=["created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        featured = f" (feat. {', '.join(self.featured_artists)})" if self.featured_artists else ""
        return f"{self.title}{featured} — {self.artist.name}"

    @property
    def duration_display(self) -> str:
        """Return duration in H:MM:SS or M:SS format if duration_seconds set."""
        if not self.duration_seconds:
            return "0:00"
        s = int(self.duration_seconds)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{sec:02d}"
        return f"{m}:{sec:02d}"
    
    @property
    def display_title(self) -> str:
        """Return formatted title with featured artists if any."""
        if self.featured_artists:
            return f"{self.title} (feat. {', '.join(self.featured_artists)})"
        return self.title

    # Play counts
    play_counts = models.ManyToManyField('PlayCount', blank=True, related_name='songs')

    # Likes
    liked_by = models.ManyToManyField(User, blank=True, related_name='liked_songs', through='SongLike')


class SongLike(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    song = models.ForeignKey('Song', on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'song')
        ordering = ['-created_at']


class Playlist(models.Model):
    """Playlist model containing songs and metadata"""
    CREATED_BY_ADMIN = 'admin'
    CREATED_BY_AUDIENCE = 'audience'
    CREATED_BY_SYSTEM = 'system'

    CREATED_BY_CHOICES = [
        (CREATED_BY_ADMIN, 'Admin'),
        (CREATED_BY_AUDIENCE, 'Audience'),
        (CREATED_BY_SYSTEM, 'System'),
    ]

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    cover_image = models.URLField(max_length=500, blank=True, null=True, help_text="R2 CDN URL for playlist cover")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.CharField(max_length=30, choices=CREATED_BY_CHOICES, default=CREATED_BY_AUDIENCE,
                                  help_text="Who created this playlist")

    # Classification
    genres = models.ManyToManyField(Genre, blank=True, related_name='playlists')
    moods = models.ManyToManyField(Mood, blank=True, related_name='playlists')
    tags = models.ManyToManyField(Tag, blank=True, related_name='playlists')

    # Songs in the playlist
    songs = models.ManyToManyField(Song, blank=True, related_name='playlists')

    # Likes
    liked_by = models.ManyToManyField(User, blank=True, related_name='liked_admin_playlists')
    saved_by = models.ManyToManyField(User, blank=True, related_name='saved_playlists')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} ({self.created_at.date()})"


class PlaylistLike(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    playlist = models.ForeignKey('Playlist', on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'playlist')
        ordering = ['-created_at']


class PlayCount(models.Model):
    """Track individual play counts for songs"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='play_counts')
    country = models.CharField(max_length=100)
    city = models.CharField(max_length=100)
    ip = models.GenericIPAddressField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"PlayCount(user={self.user_id}, {self.city}, {self.country}, {self.created_at})"


class UserPlaylist(models.Model):
    """User-created playlist model"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='user_playlists')
    title = models.CharField(max_length=255)
    public = models.BooleanField(default=False)
    liked_by = models.ManyToManyField(User, blank=True, related_name='liked_playlists')
    songs = models.ManyToManyField(Song, blank=True, related_name='user_playlists')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} by {self.user.phone_number}"


class StreamAccess(models.Model):
    """Track stream URL unwraps for ad injection logic"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='stream_accesses')
    song = models.ForeignKey(Song, on_delete=models.CASCADE, related_name='stream_accesses')
    unwrap_token = models.CharField(max_length=64, unique=True, db_index=True, null=True, blank=True, help_text="Unique token for this unwrap request (legacy)")
    short_token = models.CharField(max_length=16, unique=True, db_index=True, null=True, blank=True, help_text="Short token for URL shortening")
    # one-time access token: created when user unwraps; this token is returned to client
    # and must be used exactly once to fetch the real presigned URL (or be redirected).
    one_time_token = models.CharField(max_length=128, unique=True, db_index=True, null=True, blank=True, help_text="One-time access token returned by unwrap")
    one_time_used = models.BooleanField(default=False, help_text="Whether the one-time token was already used")
    one_time_expires_at = models.DateTimeField(null=True, blank=True, help_text="Expiry timestamp for the one-time token")
    unique_otplay_id = models.CharField(max_length=64, unique=True, db_index=True, null=True, blank=True, help_text="Unique one-time play ID for play count tracking")
    unwrapped = models.BooleanField(default=False, help_text="Whether this token has been unwrapped")
    unwrapped_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'unwrapped', 'created_at']),
            models.Index(fields=['unwrap_token']),
            models.Index(fields=['short_token']),
        ]
    
    def __str__(self):
        return f"StreamAccess(user={self.user_id}, song={self.song_id}, unwrapped={self.unwrapped})"


class RecommendedPlaylist(models.Model):
    """Auto-generated playlist recommendations based on user activity"""
    
    PLAYLIST_TYPE_SIMILAR_TASTE = 'similar_taste'
    PLAYLIST_TYPE_DISCOVER_GENRE = 'discover_genre'
    PLAYLIST_TYPE_MOOD_BASED = 'mood_based'
    PLAYLIST_TYPE_DECADE = 'decade'
    PLAYLIST_TYPE_ENERGY = 'energy'
    PLAYLIST_TYPE_ARTIST_MIX = 'artist_mix'
    
    TYPE_CHOICES = [
        (PLAYLIST_TYPE_SIMILAR_TASTE, 'Similar to Your Taste'),
        (PLAYLIST_TYPE_DISCOVER_GENRE, 'Discover Genre'),
        (PLAYLIST_TYPE_MOOD_BASED, 'Mood Based'),
        (PLAYLIST_TYPE_DECADE, 'Decade Mix'),
        (PLAYLIST_TYPE_ENERGY, 'Energy Level'),
        (PLAYLIST_TYPE_ARTIST_MIX, 'Artist Mix'),
    ]
    
    # Unique identifier for the recommended playlist
    unique_id = models.CharField(max_length=128, unique=True, db_index=True, help_text="Unique ID for this recommendation")
    
    # User for whom this playlist is recommended (null = general recommendation)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='recommended_playlists', null=True, blank=True)
    
    # Reference to the canonical Playlist object
    playlist_ref = models.ForeignKey('Playlist', on_delete=models.SET_NULL, null=True, blank=True, related_name='recommendations')

    # Playlist metadata
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    playlist_type = models.CharField(max_length=50, choices=TYPE_CHOICES, default=PLAYLIST_TYPE_SIMILAR_TASTE)
    
    # Songs in the playlist (ordered)
    songs = models.ManyToManyField(Song, related_name='recommended_playlists', blank=True)
    # Explicit ordered list of song IDs (preserves order and allows randomized ordering)
    song_order = models.JSONField(default=list, blank=True, help_text="Ordered list of song IDs for this playlist")
    
    # User interactions
    liked_by = models.ManyToManyField(User, blank=True, related_name='liked_recommended_playlists')
    saved_by = models.ManyToManyField(User, blank=True, related_name='saved_recommended_playlists')
    viewed_by = models.ManyToManyField(User, blank=True, related_name='viewed_recommended_playlists')
    views = models.PositiveIntegerField(default=0, help_text="Number of times this playlist was viewed")
    
    # Metadata for ranking/scoring
    relevance_score = models.FloatField(default=0.0, help_text="How relevant this playlist is to the user")
    match_percentage = models.FloatField(default=0.0, help_text="Percentage match with user's activity and taste (0-100)")
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Expiry (for cache invalidation)
    expires_at = models.DateTimeField(null=True, blank=True, help_text="When this recommendation expires")
    
    class Meta:
        ordering = ['-relevance_score', '-created_at']
        indexes = [
            models.Index(fields=['user', 'playlist_type', '-relevance_score']),
            models.Index(fields=['unique_id']),
            models.Index(fields=['expires_at']),
        ]
    
    def __str__(self):
        user_info = f"for {self.user.phone_number}" if self.user else "general"
        return f"{self.title} ({self.playlist_type}) {user_info}"


class EventPlaylist(models.Model):
    """Group of playlists for specific times of day (Morning, Evening, Night)"""
    TIME_MORNING = 'morning'
    TIME_EVENING = 'evening'
    TIME_NIGHT = 'night'
    
    TIME_CHOICES = [
        (TIME_MORNING, 'Morning'),
        (TIME_EVENING, 'Evening'),
        (TIME_NIGHT, 'Night'),
    ]
    
    title = models.CharField(max_length=255)
    time_of_day = models.CharField(max_length=20, choices=TIME_CHOICES)
    playlists = models.ManyToManyField(Playlist, related_name='event_playlists', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = "Event Playlist Group"
        verbose_name_plural = "Event Playlist Groups"
    
    def __str__(self):
        return f"{self.title} ({self.get_time_of_day_display()})"


class SearchSection(models.Model):
    """A section for search or home page containing songs, playlists, or albums"""
    TYPE_SONG = 'song'
    TYPE_PLAYLIST = 'playlist'
    TYPE_ALBUM = 'album'
    TYPE_CHOICES = [
        (TYPE_SONG, 'Song'),
        (TYPE_PLAYLIST, 'Playlist'),
        (TYPE_ALBUM, 'Album'),
    ]

    SIZE_SMALL = 'small'
    SIZE_MEDIUM = 'medium'
    SIZE_BIG = 'big'
    SIZE_CHOICES = [
        (SIZE_SMALL, 'Small'),
        (SIZE_MEDIUM, 'Medium'),
        (SIZE_BIG, 'Big'),
    ]

    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    title = models.CharField(max_length=255)
    icon_logo = models.URLField(max_length=500, blank=True, null=True, help_text="R2 CDN URL for icon logo")
    item_size = models.CharField(max_length=20, choices=SIZE_CHOICES, default=SIZE_MEDIUM)
    
    # Items in the section
    songs = models.ManyToManyField(Song, blank=True, related_name='search_sections')
    albums = models.ManyToManyField(Album, blank=True, related_name='search_sections')
    playlists = models.ManyToManyField(Playlist, blank=True, related_name='search_sections')

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Ownership
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_search_sections')
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='updated_search_sections')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} ({self.type})"


class UserHistory(models.Model):
    """Tracks user interactions with songs, albums, playlists, and artists"""
    TYPE_SONG = 'song'
    TYPE_ALBUM = 'album'
    TYPE_PLAYLIST = 'playlist'
    TYPE_ARTIST = 'artist'
    
    TYPE_CHOICES = [
        (TYPE_SONG, 'Song'),
        (TYPE_ALBUM, 'Album'),
        (TYPE_PLAYLIST, 'Playlist'),
        (TYPE_ARTIST, 'Artist'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='history')
    content_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    
    song = models.ForeignKey(Song, on_delete=models.CASCADE, null=True, blank=True, related_name='user_history')
    album = models.ForeignKey(Album, on_delete=models.CASCADE, null=True, blank=True, related_name='user_history')
    playlist = models.ForeignKey(Playlist, on_delete=models.CASCADE, null=True, blank=True, related_name='user_history')
    artist = models.ForeignKey(Artist, on_delete=models.CASCADE, null=True, blank=True, related_name='user_history')
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name_plural = "User Histories"
        # Ensure we only have one history record per user per item
        # We'll update the updated_at field on subsequent views
        indexes = [
            models.Index(fields=['user', 'content_type', '-updated_at']),
        ]

    def __str__(self):
        item = self.song or self.album or self.playlist or self.artist
        return f"{self.user.phone_number} viewed {self.content_type}: {item}"


class Rules(models.Model):
    title = models.CharField(max_length=255)
    content = models.TextField()
    version = models.CharField(max_length=20, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} (v{self.version})"

    def save(self, *args, **kwargs):
        if not self.version:
            self.version = self._generate_next_version()
        super().save(*args, **kwargs)

    @staticmethod
    def _generate_next_version():
        latest = Rules.objects.order_by('-created_at').first()
        if not latest:
            return "1.0.0"
        
        # Parse version like 1.0.0
        parts = latest.version.split('.')
        if len(parts) != 3:
            return "1.0.0"  # fallback
        
        major, minor, patch = map(int, parts)
        
        # Increment patch
        patch += 1
        if patch > 9:
            patch = 0
            minor += 1
            if minor > 9:
                minor = 0
                major += 1
        
        return f"{major}.{minor}.{patch}"
