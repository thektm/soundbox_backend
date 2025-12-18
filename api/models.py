from django.db import models
from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.core.validators import MinValueValidator, MaxValueValidator


class UserManager(BaseUserManager):
    def create_user(self, phone_number, password=None, roles='audience', **extra_fields):
        if not phone_number:
            raise ValueError('The phone number must be set')
        phone_number = str(phone_number)
        user = self.model(phone_number=phone_number, roles=roles, **extra_fields)
        user.set_password(password)
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

    # users this user follows; reverse accessor 'followers' gives users following this user
    followings = models.ManyToManyField('self', symmetrical=False, related_name='followers', blank=True)

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

    # user-specific settings stored as JSON
    settings = models.JSONField(default=dict, blank=True)

    objects = UserManager()

    USERNAME_FIELD = 'phone_number'
    REQUIRED_FIELDS = []

    def __str__(self):
        return self.phone_number


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
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"RefreshToken(user={self.user_id} expires={self.expires_at} revoked={self.revoked_at is not None})"


class Artist(models.Model):
    """Artist model - can be linked to a user account or standalone"""
    name = models.CharField(max_length=255, unique=True)
    user = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='artist_profile')
    bio = models.TextField(blank=True)
    profile_image = models.URLField(max_length=500, blank=True, help_text="R2 CDN URL for profile image")
    verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['name']
    
    def __str__(self):
        return self.name


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

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} ({self.created_at.date()})"


class PlayCount(models.Model):
    """Track individual song plays with geographical data"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='play_counts')
    song = models.ForeignKey(Song, on_delete=models.CASCADE, related_name='play_counts')
    country = models.CharField(max_length=100, blank=True)
    city = models.CharField(max_length=100, blank=True)
    ip = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['song', 'created_at']),
            models.Index(fields=['user', 'created_at']),
            models.Index(fields=['country', 'city']),
        ]
    
    def __str__(self):
        return f"PlayCount(user={self.user_id}, song={self.song_id}, country={self.country}, city={self.city})"


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
    # unique one-time play ID for tracking play count submission
    unique_otplay_id = models.CharField(max_length=128, unique=True, db_index=True, null=True, blank=True, help_text="Unique one-time play ID for play count tracking")
    otplay_id_used = models.BooleanField(default=False, help_text="Whether the unique_otplay_id was already used to record a play")
    unwrapped = models.BooleanField(default=False, help_text="Whether this token has been unwrapped")
    unwrapped_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'unwrapped', 'created_at']),
            models.Index(fields=['unwrap_token']),
            models.Index(fields=['short_token']),
            models.Index(fields=['unique_otplay_id']),
        ]
    
    def __str__(self):
        return f"StreamAccess(user={self.user_id}, song={self.song_id}, unwrapped={self.unwrapped})"
