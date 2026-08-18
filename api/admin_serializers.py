from decimal import Decimal

from rest_framework import serializers
from .models import (
    User, Artist, ArtistAuth, ArtistSocialAccount, NotificationSetting, Song, Album, Genre, SubGenre, 
    Mood, Tag, Report, PlayConfiguration, BannerAd, AudioAd, PaymentTransaction, 
    DepositRequest, SearchSection, EventPlaylist, Playlist, SupportTicket, SongPromotion
)
from django.contrib.auth import get_user_model
from django.conf import settings
from django.db import transaction
from django.utils.text import slugify
from .song_play_metrics import hydrate_song_play_counts
from .utils import generate_signed_r2_url, public_media_url, r2_object_key
from .admin_permissions import (
    ALLOWED_PERMISSION_KEYS, employee_permissions_payload, employee_session_version,
    has_employee_permission, is_employee, normalize_employee_permissions,
)

User = get_user_model()

class AdminSignedMediaSerializerMixin:
    """Marker for admin serializers; media is signed once by response middleware."""


class RequireEnglishTranslationSerializerMixin:
    """Require explicit English copy for admin-authored visible text.

    This applies to create requests and to updates that touch either side of a
    translation pair. Unrelated partial updates remain backward compatible.
    """

    translation_pairs = ()

    def validate(self, attrs):
        attrs = super().validate(attrs)
        errors = {}
        is_create = self.instance is None
        for source_name, english_name in self.translation_pairs:
            if not is_create and source_name not in attrs and english_name not in attrs:
                continue
            source_value = attrs.get(source_name, getattr(self.instance, source_name, None))
            english_value = attrs.get(english_name, getattr(self.instance, english_name, None))
            if source_value and not str(english_value or "").strip():
                errors[english_name] = "Enter the real English equivalent; transliteration/Finglish is not accepted."
        if errors:
            raise serializers.ValidationError(errors)
        return attrs

class AdminTaxonomySerializer(serializers.Serializer):
    """Strict admin writer for the four catalog taxonomy models.

    Public taxonomy serializers intentionally stay permissive for backwards
    compatibility.  The admin workspace uses this serializer so newly-authored
    taxonomy remains bilingual, normalized and collision-free.
    """

    name = serializers.CharField(max_length=100, trim_whitespace=True)
    name_en = serializers.CharField(max_length=100, trim_whitespace=True)
    slug = serializers.CharField(max_length=100, required=False, allow_blank=True, trim_whitespace=True)
    parent_genre = serializers.PrimaryKeyRelatedField(
        queryset=Genre.objects.all(), required=False, allow_null=True
    )

    MODEL_BY_KIND = {
        'genre': Genre,
        'subgenre': SubGenre,
        'mood': Mood,
        'tag': Tag,
    }

    def _kind(self):
        kind = str(self.context.get('kind') or '').strip().lower()
        if kind not in self.MODEL_BY_KIND:
            raise serializers.ValidationError({'kind': 'نوع دسته‌بندی معتبر نیست.'})
        return kind

    @staticmethod
    def _normalized_text(value):
        return ' '.join(str(value or '').strip().split())

    def validate(self, attrs):
        kind = self._kind()
        model = self.MODEL_BY_KIND[kind]
        instance = self.instance

        current_name = getattr(instance, 'name', '') if instance is not None else ''
        current_name_en = getattr(instance, 'name_en', '') if instance is not None else ''
        current_slug = getattr(instance, 'slug', '') if instance is not None else ''

        name = self._normalized_text(attrs.get('name', current_name))
        name_en = self._normalized_text(attrs.get('name_en', current_name_en))
        if not name:
            raise serializers.ValidationError({'name': 'نام فارسی الزامی است.'})
        if not name_en:
            raise serializers.ValidationError({'name_en': 'نام انگلیسی الزامی است.'})

        requested_slug = str(attrs.get('slug', '') or '').strip().lower()
        if requested_slug:
            clean_slug = slugify(requested_slug, allow_unicode=False)
        elif instance is not None and 'name_en' not in attrs and current_slug:
            clean_slug = current_slug
        else:
            clean_slug = slugify(name_en, allow_unicode=False)
        if not clean_slug:
            raise serializers.ValidationError({'slug': 'برای ساخت شناسه URL یک نام انگلیسی معتبر وارد کنید.'})
        if len(clean_slug) > 100:
            raise serializers.ValidationError({'slug': 'شناسه URL حداکثر ۱۰۰ کاراکتر است.'})

        base = model.objects.all()
        if instance is not None:
            base = base.exclude(pk=instance.pk)
        errors = {}
        if base.filter(name__iexact=name).exists():
            errors['name'] = 'یک مورد با این نام فارسی از قبل وجود دارد.'
        if base.filter(name_en__iexact=name_en).exists():
            errors['name_en'] = 'یک مورد با این نام انگلیسی از قبل وجود دارد.'
        if base.filter(slug__iexact=clean_slug).exists():
            errors['slug'] = 'این شناسه URL از قبل استفاده شده است.'
        if errors:
            raise serializers.ValidationError(errors)

        attrs['name'] = name
        attrs['name_en'] = name_en
        attrs['slug'] = clean_slug

        if kind == 'subgenre':
            parent = attrs.get('parent_genre', getattr(instance, 'parent_genre', None))
            if parent is None:
                raise serializers.ValidationError({'parent_genre': 'انتخاب ژانر مادر برای زیرژانر الزامی است.'})
        else:
            attrs.pop('parent_genre', None)
        return attrs

    def create(self, validated_data):
        model = self.MODEL_BY_KIND[self._kind()]
        return model.objects.create(**validated_data)

    def update(self, instance, validated_data):
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save(update_fields=[*validated_data.keys()])
        return instance


class AdminUserSerializer(serializers.ModelSerializer):
    has_artist_profile = serializers.SerializerMethodField()
    artist_verified = serializers.SerializerMethodField()

    def get_has_artist_profile(self, obj):
        return hasattr(obj, 'artist_profile')

    def get_artist_verified(self, obj):
        profile = getattr(obj, 'artist_profile', None)
        return profile.verified if profile is not None else None

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context.get('request')
        user = getattr(request, 'user', None) if request is not None else None
        if is_employee(user):
            submitted = set(getattr(self, 'initial_data', {}) or {})
            # Employee user editing is deliberately narrower than the owner serializer.
            # Account roles, plan/verification and security state are owner workflows;
            # ban/unban is handled only by the dedicated, object-checked endpoint.
            editable = {'first_name', 'last_name', 'email', 'stream_quality'}
            forbidden = submitted - editable
            if forbidden:
                raise serializers.ValidationError({
                    field: 'این فیلد از بخش ویرایش کاربر قابل تغییر نیست.' for field in forbidden
                })
        return attrs

    class Meta:
        model = User
        fields = [
            'id', 'phone_number', 'unique_id', 'first_name', 'last_name', 'email',
            'roles', 'is_active', 'is_banned', 'is_staff', 'is_verified', 'date_joined',
            'plan', 'stream_quality', 'last_login_at', 'failed_login_attempts', 'locked_until',
            'has_artist_profile', 'artist_verified'
        ]
        read_only_fields = ['id', 'unique_id', 'date_joined', 'last_login_at']

class AdminEmployeeSerializer(serializers.ModelSerializer):
    role = serializers.ChoiceField(
        choices=[User.ROLE_MANAGER, User.ROLE_SUPERVISOR], required=False
    )
    password = serializers.CharField(
        write_only=True, required=False, min_length=8, trim_whitespace=False
    )

    class Meta:
        model = User
        fields = [
            'id', 'phone_number', 'first_name', 'last_name', 'email',
            'role', 'roles', 'is_active', 'date_joined', 'last_login_at',
            'permissions', 'password'
        ]
        read_only_fields = ['id', 'roles', 'date_joined', 'last_login_at']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        roles = list(instance.roles or [])
        if User.ROLE_MANAGER in roles:
            data['role'] = User.ROLE_MANAGER
        elif User.ROLE_SUPERVISOR in roles:
            data['role'] = User.ROLE_SUPERVISOR
        else:
            data['role'] = None
        data['permissions'] = normalize_employee_permissions(instance.permissions)
        return data

    @staticmethod
    def _ascii_digits(value):
        import unicodedata
        output = []
        for char in str(value or ''):
            try:
                output.append(str(unicodedata.digit(char)))
            except (TypeError, ValueError):
                if char.isascii() and char.isdigit():
                    output.append(char)
        return ''.join(output)

    def validate_phone_number(self, value):
        digits = self._ascii_digits(value)
        if digits.startswith('0098') and len(digits) == 14:
            digits = '0' + digits[4:]
        elif digits.startswith('98') and len(digits) == 12:
            digits = '0' + digits[2:]
        if len(digits) != 11 or not digits.startswith('09'):
            raise serializers.ValidationError('شماره همراه را به‌صورت 09xxxxxxxxx وارد کنید.')
        qs = User.objects.filter(phone_number=digits)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError('این شماره همراه قبلاً در سیستم ثبت شده است.')
        return digits

    def validate_permissions(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('ساختار دسترسی‌ها معتبر نیست.')
        unknown = set(value) - ALLOWED_PERMISSION_KEYS
        if unknown:
            raise serializers.ValidationError('یک یا چند دسترسی ناشناخته ارسال شده است.')
        return normalize_employee_permissions(value)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance is None:
            if not attrs.get('password'):
                raise serializers.ValidationError({'password': 'رمز عبور اولیه الزامی است.'})
            attrs.setdefault('role', User.ROLE_SUPERVISOR)
            attrs['permissions'] = normalize_employee_permissions(attrs.get('permissions', {}))
        elif 'password' in attrs:
            raise serializers.ValidationError({
                'password': 'برای تغییر رمز عبور از بخش مخصوص تغییر رمز استفاده کنید.'
            })
        return attrs

    def create(self, validated_data):
        password = validated_data.pop('password')
        role = validated_data.pop('role', User.ROLE_SUPERVISOR)
        permissions_value = employee_permissions_payload(
            validated_data.pop('permissions', {}), session_version=1
        )
        return User.objects.create_user(
            password=password,
            roles=[role],
            permissions=permissions_value,
            is_staff=False,
            is_superuser=False,
            is_verified=True,
            is_banned=False,
            **validated_data,
        )

    def update(self, instance, validated_data):
        role = validated_data.pop('role', None)
        permissions_present = 'permissions' in validated_data
        permissions_value = validated_data.pop('permissions', None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if role is not None:
            instance.roles = [role]
        if permissions_present:
            instance.permissions = employee_permissions_payload(
                permissions_value,
                session_version=employee_session_version(instance),
            )
        instance.is_staff = False
        instance.is_superuser = False
        instance.is_banned = False
        instance.is_verified = True
        instance.save()
        return instance

class AdminArtistSerializer(AdminSignedMediaSerializerMixin, RequireEnglishTranslationSerializerMixin, serializers.ModelSerializer):
    translation_pairs = (('name', 'name_en'), ('artistic_name', 'artistic_name_en'), ('city', 'city_en'), ('address', 'address_en'), ('bio', 'bio_en'))
    has_user = serializers.SerializerMethodField()
    user_phone = serializers.CharField(source='user.phone_number', read_only=True)
    user_is_banned = serializers.BooleanField(source='user.is_banned', read_only=True, allow_null=True)
    social_accounts = serializers.SerializerMethodField()

    class Meta:
        model = Artist
        fields = [
            'id', 'name', 'name_en', 'artistic_name', 'artistic_name_en', 'unique_id', 'email', 'city', 'city_en', 'date_of_birth',
            'address', 'address_en', 'id_number', 'user', 'user_phone', 'user_is_banned', 'bio', 'bio_en', 'profile_image',
            'banner_image', 'verified', 'created_at', 'has_user', 'social_accounts'
        ]
        read_only_fields = ['id', 'created_at']

    def get_has_user(self, obj):
        return obj.user is not None

    def get_social_accounts(self, obj):
        links = obj.social_account_links.select_related('platform').all()
        return [{'id': link.id, 'platform': link.platform_id, 'platform_name': link.platform.name, 'platform_slug': link.platform.slug, 'username': link.username, 'url': link.url} for link in links]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get('request')
        user = getattr(request, 'user', None) if request is not None else None
        if is_employee(user) and not has_employee_permission(user, 'artists.kyc'):
            for field in ('date_of_birth', 'address', 'address_en', 'id_number'):
                data[field] = None
            data['kyc_hidden'] = True
        else:
            data['kyc_hidden'] = False
        return data

class AdminArtistAuthSerializer(AdminSignedMediaSerializerMixin, serializers.ModelSerializer):
    profile_image = serializers.SerializerMethodField()
    national_id_image = serializers.SerializerMethodField()

    def _verification_media(self, obj, field_name):
        value = getattr(obj, field_name, None)
        if not value:
            return None
        raw = str(getattr(value, 'name', '') or '').strip()
        if not raw:
            return None
        if r2_object_key(raw, allow_key=False):
            return generate_signed_r2_url(
                raw, expiration=getattr(settings, 'ADMIN_R2_SIGNED_URL_TTL', 3600)
            ) or raw
        return public_media_url(self.context.get('request'), value) or raw

    def get_profile_image(self, obj):
        return self._verification_media(obj, 'profile_image')

    def get_national_id_image(self, obj):
        return self._verification_media(obj, 'national_id_image')

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get('request')
        user = getattr(request, 'user', None) if request is not None else None
        if is_employee(user) and not has_employee_permission(user, 'artists.kyc'):
            for field in ('national_id', 'national_id_image', 'birth_date', 'address', 'address_en'):
                data[field] = None
            data['kyc_hidden'] = True
        else:
            data['kyc_hidden'] = False
        return data

    class Meta:
        model = ArtistAuth
        fields = [
            'id', 'user', 'auth_type', 'artist_claimed',
            'first_name', 'first_name_en', 'last_name', 'last_name_en',
            'stage_name', 'stage_name_en', 'birth_date', 'national_id',
            'phone_number', 'email', 'city', 'city_en', 'address', 'address_en',
            'biography', 'biography_en', 'profile_image', 'national_id_image',
            'status', 'is_verified', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate(self, attrs):
        status_value = attrs.get('status', getattr(self.instance, 'status', ArtistAuth.STATUS_PENDING))
        verified = attrs.get('is_verified', getattr(self.instance, 'is_verified', False))
        auth_type = attrs.get('auth_type', getattr(self.instance, 'auth_type', ArtistAuth.AUTH_FRESH))
        user = attrs.get('user', getattr(self.instance, 'user', None))
        claimed = attrs.get('artist_claimed', getattr(self.instance, 'artist_claimed', None))
        if status_value == ArtistAuth.STATUS_ACCEPTED or verified:
            if not user:
                raise serializers.ValidationError({'user': 'Approval requires a linked user.'})
            if auth_type == ArtistAuth.AUTH_EXISTING:
                if not claimed:
                    raise serializers.ValidationError({'artist_claimed': 'Select the existing artist before approval.'})
                if claimed.user_id not in (None, user.pk):
                    raise serializers.ValidationError({'artist_claimed': 'This artist is linked to another user.'})
                if Artist.objects.filter(user=user).exclude(pk=claimed.pk).exists():
                    raise serializers.ValidationError({'user': 'This user is linked to another artist profile.'})
        return attrs

class AdminSongSerializer(AdminSignedMediaSerializerMixin, RequireEnglishTranslationSerializerMixin, serializers.ModelSerializer):
    translation_pairs = (('title', 'title_en'), ('description', 'description_en'), ('lyrics', 'lyrics_en'), ('label', 'label_en'), ('credits', 'credits_en'))
    # We use FileField for uploads, but the model stores URLField
    audio_file_upload = serializers.FileField(write_only=True, required=False)
    cover_image_upload = serializers.ImageField(write_only=True, required=False)
    
    # Display fields
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    album_title = serializers.CharField(source='album.title', read_only=True, allow_null=True)
    plays = serializers.SerializerMethodField()
    likes_count = serializers.SerializerMethodField()
    metadata_completion = serializers.SerializerMethodField()
    genre_names = serializers.SerializerMethodField()
    sub_genre_names = serializers.SerializerMethodField()
    mood_names = serializers.SerializerMethodField()

    AUDIO_CLASSIFICATION_FIELDS = (
        'tempo', 'energy', 'danceability', 'valence', 'acousticness',
        'instrumentalness', 'speechiness',
    )

    # Relationship details
    featured_artists = serializers.SerializerMethodField()
    featured_artist_ids = serializers.PrimaryKeyRelatedField(
        queryset=Artist.objects.all(),
        many=True,
        source='featured_artists',
        required=False,
        write_only=True
    )

    def get_featured_artists(self, obj):
        return [{'id': a.id, 'name': a.name, 'artistic_name': a.artistic_name} for a in obj.featured_artists.all()]

    def get_plays(self, obj):
        annotated = getattr(obj, 'total_plays', None)
        if annotated is not None:
            return int(annotated or 0)
        tracked = getattr(obj, '_cached_tracked_plays', None)
        if tracked is None:
            tracked = getattr(obj, 'tracked_plays', None)
        if tracked is not None:
            return int(obj.plays or 0) + int(tracked or 0)
        return int(obj.plays or 0)

    def get_likes_count(self, obj):
        annotated = getattr(obj, 'likes_count', None)
        return int(annotated if annotated is not None else obj.liked_by.count())

    @staticmethod
    def _names(manager):
        return [item.name for item in manager.all()]

    def get_genre_names(self, obj):
        return self._names(obj.genres)

    def get_sub_genre_names(self, obj):
        return self._names(obj.sub_genres)

    def get_mood_names(self, obj):
        return self._names(obj.moods)

    def get_metadata_completion(self, obj):
        genre_ready = bool(self.get_genre_names(obj))
        mood_ready = bool(self.get_mood_names(obj))
        audio_ready = sum(getattr(obj, field, None) is not None for field in self.AUDIO_CLASSIFICATION_FIELDS)
        score = (int(genre_ready) + int(mood_ready) + audio_ready / len(self.AUDIO_CLASSIFICATION_FIELDS)) / 3
        return int(round(score * 100))

    # JSON fields as ListFields for better form-data handling
    producers = serializers.ListField(child=serializers.CharField(), required=False)
    producers_en = serializers.ListField(child=serializers.CharField(), required=False)
    composers = serializers.ListField(child=serializers.CharField(), required=False)
    composers_en = serializers.ListField(child=serializers.CharField(), required=False)
    lyricists = serializers.ListField(child=serializers.CharField(), required=False)
    lyricists_en = serializers.ListField(child=serializers.CharField(), required=False)

    class Meta:
        model = Song
        fields = [
            'id', 'title', 'title_en', 'artist', 'artist_name', 'featured_artists', 'featured_artist_ids', 'album', 'album_title',
            'is_single', 'album_disc_number', 'album_track_number', 'audio_file', 'converted_audio_url', 'cover_image', 'original_format',
            'duration_seconds', 'plays', 'likes_count', 'metadata_completion', 'status', 'release_date', 'language',
            'genres', 'sub_genres', 'moods', 'tags', 'genre_names', 'sub_genre_names', 'mood_names', 'description', 'description_en', 'lyrics', 'lyrics_en',
            'tempo', 'energy', 'danceability', 'valence', 'acousticness',
            'instrumentalness', 'live_performed', 'speechiness', 'label', 'label_en',
            'producers', 'producers_en', 'composers', 'composers_en', 'lyricists', 'lyricists_en', 'credits', 'credits_en', 'uploader',
            'created_at', 'updated_at', 'audio_file_upload', 'cover_image_upload'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'plays']


class AdminReportSerializer(serializers.ModelSerializer):
    user_phone = serializers.CharField(source='user.phone_number', read_only=True)
    user_is_banned = serializers.BooleanField(source='user.is_banned', read_only=True, allow_null=True)
    song_title = serializers.CharField(source='song.title', read_only=True, allow_null=True)
    artist_name = serializers.CharField(source='artist.name', read_only=True, allow_null=True)
    reported_user_phone = serializers.CharField(source='reported_user.phone_number', read_only=True, allow_null=True)
    reported_user_unique_id = serializers.CharField(source='reported_user.unique_id', read_only=True, allow_null=True)
    reported_user_name = serializers.SerializerMethodField()

    class Meta:
        model = Report
        fields = [
            'id', 'user', 'user_phone', 'user_is_banned', 'song', 'song_title', 'artist', 'artist_name', 'reported_user', 'reported_user_phone',
            'reported_user_unique_id', 'reported_user_name', 'text', 'has_reviewed', 'reviewed_at', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user', 'song', 'artist', 'reported_user', 'created_at', 'updated_at']

    def get_reported_user_name(self, obj):
        user = obj.reported_user
        if not user:
            return ''
        full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        return full_name or user.unique_id or user.phone_number


class AdminAlbumSerializer(AdminSignedMediaSerializerMixin, RequireEnglishTranslationSerializerMixin, serializers.ModelSerializer):
    translation_pairs = (('title', 'title_en'), ('description', 'description_en'))
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    songs = serializers.SerializerMethodField()
    is_removed = serializers.SerializerMethodField()
    cover_image_upload = serializers.ImageField(write_only=True, required=False)
    
    def _admin_songs(self, obj):
        prefetched = getattr(obj, '_admin_visible_songs', None)
        if prefetched is not None:
            return list(prefetched)
        return list(
            obj.songs.exclude(status=Song.STATUS_DRAFT)
            .select_related('artist', 'album')
            .prefetch_related('featured_artists', 'genres', 'sub_genres', 'moods', 'tags')
        )

    def get_songs(self, obj):
        return AdminSongSerializer(self._admin_songs(obj), many=True).data

    def get_is_removed(self, obj):
        active_count = getattr(obj, 'active_song_count', None)
        if active_count is not None:
            return int(active_count) == 0
        songs = self._admin_songs(obj)
        return bool(songs) and all(song.status == Song.STATUS_DELETED for song in songs)

    # For write operations
    genres = serializers.PrimaryKeyRelatedField(queryset=Genre.objects.all(), many=True, required=False)
    sub_genres = serializers.PrimaryKeyRelatedField(queryset=SubGenre.objects.all(), many=True, required=False)
    moods = serializers.PrimaryKeyRelatedField(queryset=Mood.objects.all(), many=True, required=False)

    class Meta:
        model = Album
        fields = [
            'id', 'title', 'title_en', 'artist', 'artist_name', 'cover_image', 'cover_image_upload',
            'release_date', 'description', 'description_en', 'genres', 'sub_genres', 'moods',
            'created_at', 'songs', 'is_removed'
        ]
        read_only_fields = ['id', 'cover_image', 'created_at']


class AdminPlayConfigurationSerializer(serializers.ModelSerializer):
    per_normal_play_pay = serializers.DecimalField(source='free_play_worth', max_digits=12, decimal_places=8, min_value=0)
    per_premium_play_pay = serializers.DecimalField(source='premium_play_worth', max_digits=12, decimal_places=8, min_value=0)
    minimum_payout_amount = serializers.DecimalField(
        max_digits=15,
        decimal_places=2,
        min_value=Decimal('0.01'),
    )

    class Meta:
        model = PlayConfiguration
        fields = [
            'premium_plan_price', 'per_normal_play_pay', 'per_premium_play_pay',
            'minimum_payout_amount', 'ad_frequency', 'updated_at'
        ]
        read_only_fields = ['updated_at']


class AdminBannerAdSerializer(AdminSignedMediaSerializerMixin, RequireEnglishTranslationSerializerMixin, serializers.ModelSerializer):
    translation_pairs = (('title', 'title_en'),)
    image_upload = serializers.ImageField(write_only=True, required=False)

    class Meta:
        model = BannerAd
        fields = ['id', 'title', 'title_en', 'image', 'image_upload', 'navigate_link', 'is_active', 'created_at', 'updated_at']
        read_only_fields = ['id', 'image', 'created_at', 'updated_at']


class AdminAudioAdSerializer(AdminSignedMediaSerializerMixin, RequireEnglishTranslationSerializerMixin, serializers.ModelSerializer):
    translation_pairs = (('title', 'title_en'),)
    audio_upload = serializers.FileField(write_only=True, required=False)
    image_cover_upload = serializers.ImageField(write_only=True, required=False)
    audio_url = serializers.CharField(required=False, allow_blank=True)
    image_cover = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    class Meta:
        model = AudioAd
        fields = [
            'id', 'title', 'title_en', 'audio_url', 'audio_upload', 'image_cover', 'image_cover_upload',
            'navigate_link', 'duration', 'skippable_after', 'is_active', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class AdminPaymentTransactionSerializer(RequireEnglishTranslationSerializerMixin, serializers.ModelSerializer):
    translation_pairs = (('description', 'description_en'),)
    user_phone = serializers.CharField(source='user.phone_number', read_only=True)
    user_plan = serializers.CharField(source='user.plan', read_only=True)

    class Meta:
        model = PaymentTransaction
        fields = ['id', 'user', 'user_phone', 'user_plan', 'transaction_id', 'amount', 'status', 'payment_method', 'description', 'description_en', 'created_at']
        read_only_fields = ['id', 'created_at']


class AdminDepositRequestSerializer(serializers.ModelSerializer):
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    artist_phone = serializers.CharField(source='artist.user.phone_number', read_only=True, allow_null=True)

    class Meta:
        model = DepositRequest
        fields = ['id', 'artist', 'artist_name', 'artist_phone', 'amount', 'status', 'transaction_id', 'submission_date', 'status_change_date', 'summary']
        read_only_fields = ['id', 'submission_date', 'status_change_date']


class AdminPlaylistSerializer(AdminSignedMediaSerializerMixin, RequireEnglishTranslationSerializerMixin, serializers.ModelSerializer):
    translation_pairs = (('title', 'title_en'), ('description', 'description_en'))
    cover_image_upload = serializers.ImageField(write_only=True, required=False)
    likes_count = serializers.IntegerField(source='liked_by.count', read_only=True)
    saves_count = serializers.IntegerField(source='saved_by.count', read_only=True)
    songs = serializers.SerializerMethodField()
    song_ids = serializers.PrimaryKeyRelatedField(
        queryset=Song.objects.filter(status=Song.STATUS_PUBLISHED),
        many=True,
        source='songs',
        write_only=True,
        required=False,
    )
    song_details = serializers.SerializerMethodField()

    @staticmethod
    def _ordered_songs(obj):
        cache_name = '_admin_ordered_songs_cache'
        cached = getattr(obj, cache_name, None)
        if cached is not None:
            return cached
        through = Playlist.songs.through
        ordered_ids = list(
            through.objects.filter(playlist_id=obj.pk)
            .order_by('pk')
            .values_list('song_id', flat=True)
        )
        if not ordered_ids:
            ordered = []
        else:
            song_queryset = (
                obj.songs.select_related('artist', 'album')
                .prefetch_related('featured_artists', 'genres', 'sub_genres', 'moods', 'tags')
            )
            songs = {song.id: song for song in song_queryset}
            ordered = [songs[song_id] for song_id in ordered_ids if song_id in songs]
        setattr(obj, cache_name, ordered)
        return ordered

    def get_songs(self, obj):
        return [song.id for song in self._ordered_songs(obj)]

    def get_song_details(self, obj):
        if not self.context.get('include_song_details'):
            return []
        songs = hydrate_song_play_counts(self._ordered_songs(obj))
        return AdminSongSerializer(songs, many=True).data
    
    class Meta:
        model = Playlist
        fields = [
            'id', 'title', 'title_en', 'description', 'description_en', 'cover_image',
            'cover_image_upload', 'created_by', 'songs', 'song_ids', 'song_details', 'genres', 'moods', 'tags',
            'likes_count', 'saves_count', 'created_at'
        ]
        read_only_fields = ['id', 'cover_image', 'created_at']


class AdminSearchSectionSerializer(AdminSignedMediaSerializerMixin, RequireEnglishTranslationSerializerMixin, serializers.ModelSerializer):
    translation_pairs = (('title', 'title_en'),)
    icon_logo_upload = serializers.ImageField(write_only=True, required=False)
    item_count = serializers.SerializerMethodField()
    item_details = serializers.SerializerMethodField()
    item_ids = serializers.CharField(write_only=True, required=False, allow_blank=True)

    def _manager(self, obj):
        return {'song': obj.songs, 'album': obj.albums, 'playlist': obj.playlists}.get(obj.type)

    def get_item_count(self, obj):
        manager = self._manager(obj)
        return manager.count() if manager is not None else 0

    def get_item_details(self, obj):
        manager = self._manager(obj)
        if manager is None:
            return []
        if obj.type == SearchSection.TYPE_SONG:
            return [
                {'id': item.id, 'title': item.title, 'subtitle': item.artist.name, 'image': item.cover_image}
                for item in manager.select_related('artist').all()
            ]
        if obj.type == SearchSection.TYPE_ALBUM:
            return [
                {'id': item.id, 'title': item.title, 'subtitle': item.artist.name, 'image': item.cover_image}
                for item in manager.select_related('artist').all()
            ]
        return [
            {'id': item.id, 'title': item.title, 'subtitle': '', 'image': item.cover_image}
            for item in manager.all()
        ]

    @staticmethod
    def _parse_item_ids(value):
        result = []
        seen = set()
        for raw in str(value or '').split(','):
            try:
                item_id = int(raw)
            except (TypeError, ValueError):
                continue
            if item_id > 0 and item_id not in seen:
                seen.add(item_id)
                result.append(item_id)
        return result

    def _set_items(self, instance, raw_ids):
        ids = self._parse_item_ids(raw_ids)
        instance.songs.clear(); instance.albums.clear(); instance.playlists.clear()
        if instance.type == SearchSection.TYPE_SONG:
            instance.songs.set(Song.objects.filter(id__in=ids, status=Song.STATUS_PUBLISHED))
        elif instance.type == SearchSection.TYPE_ALBUM:
            instance.albums.set(
                Album.objects.filter(id__in=ids, songs__status=Song.STATUS_PUBLISHED).distinct()
            )
        elif instance.type == SearchSection.TYPE_PLAYLIST:
            instance.playlists.set(Playlist.objects.filter(id__in=ids))

    def create(self, validated_data):
        raw_ids = validated_data.pop('item_ids', None)
        instance = super().create(validated_data)
        if raw_ids is not None:
            self._set_items(instance, raw_ids)
        return instance

    def update(self, instance, validated_data):
        raw_ids = validated_data.pop('item_ids', None)
        instance = super().update(instance, validated_data)
        if raw_ids is not None:
            self._set_items(instance, raw_ids)
        return instance
    
    class Meta:
        model = SearchSection
        fields = [
            'id', 'type', 'title', 'title_en', 'icon_logo', 'icon_logo_upload', 'item_size',
            'songs', 'albums', 'playlists', 'item_ids', 'item_count', 'item_details', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'icon_logo', 'created_at', 'updated_at']


class AdminEventPlaylistSerializer(AdminSignedMediaSerializerMixin, RequireEnglishTranslationSerializerMixin, serializers.ModelSerializer):
    translation_pairs = (('title', 'title_en'),)
    cover_image_upload = serializers.ImageField(write_only=True, required=False)
    playlist_details = serializers.SerializerMethodField()

    @staticmethod
    def _ordered_playlists(obj):
        through = EventPlaylist.playlists.through
        ordered_ids = list(
            through.objects.filter(eventplaylist_id=obj.pk)
            .order_by('pk')
            .values_list('playlist_id', flat=True)
        )
        playlist_map = {playlist.id: playlist for playlist in obj.playlists.all()}
        return [playlist_map[playlist_id] for playlist_id in ordered_ids if playlist_id in playlist_map]

    @staticmethod
    def _set_playlist_order(instance, playlists):
        through = EventPlaylist.playlists.through
        through.objects.filter(eventplaylist_id=instance.pk).delete()
        for playlist in playlists:
            through.objects.create(eventplaylist_id=instance.pk, playlist_id=playlist.pk)

    def get_playlist_details(self, obj):
        return AdminPlaylistSerializer(self._ordered_playlists(obj), many=True).data

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance is None and 'playlists' not in attrs:
            raise serializers.ValidationError({'playlists': 'Select at least 1 playlist.'})
        if 'playlists' in attrs:
            playlist_ids = [playlist.pk for playlist in attrs['playlists']]
            if not 1 <= len(playlist_ids) <= 3:
                raise serializers.ValidationError({'playlists': 'Select between 1 and 3 playlists.'})
            if len(set(playlist_ids)) != len(playlist_ids):
                raise serializers.ValidationError({'playlists': 'Playlists must be distinct.'})
        time_of_day = attrs.get('time_of_day', getattr(self.instance, 'time_of_day', None))
        if self.instance is None and time_of_day and EventPlaylist.objects.filter(time_of_day=time_of_day).exists():
            raise serializers.ValidationError({'time_of_day': 'A configuration for this time of day already exists.'})
        if self.instance is not None and 'time_of_day' in attrs and attrs['time_of_day'] != self.instance.time_of_day:
            if EventPlaylist.objects.filter(time_of_day=attrs['time_of_day']).exclude(pk=self.instance.pk).exists():
                raise serializers.ValidationError({'time_of_day': 'A configuration for this time of day already exists.'})
        return attrs

    def create(self, validated_data):
        playlists = list(validated_data.pop('playlists', []))
        with transaction.atomic():
            instance = super().create(validated_data)
            self._set_playlist_order(instance, playlists)
        return instance

    def update(self, instance, validated_data):
        has_playlists = 'playlists' in validated_data
        playlists = list(validated_data.pop('playlists', [])) if has_playlists else []
        with transaction.atomic():
            instance = super().update(instance, validated_data)
            if has_playlists:
                self._set_playlist_order(instance, playlists)
        return instance

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['playlists'] = [playlist.id for playlist in self._ordered_playlists(instance)]
        return data

    class Meta:
        model = EventPlaylist
        fields = [
            'id', 'title', 'title_en', 'time_of_day', 'cover_image', 'cover_image_upload',
            'playlists', 'playlist_details', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'cover_image', 'playlist_details', 'created_at', 'updated_at']



class AdminSupportTicketSerializer(serializers.ModelSerializer):
    user_phone = serializers.CharField(source='user.phone_number', read_only=True)
    artist_name = serializers.SerializerMethodField()
    responded_by_phone = serializers.CharField(source='responded_by.phone_number', read_only=True, allow_null=True)

    class Meta:
        model = SupportTicket
        fields = [
            'id', 'user', 'user_phone', 'artist_name', 'subject', 'message', 'status',
            'admin_response', 'responded_by', 'responded_by_phone', 'responded_at',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'user', 'responded_by', 'responded_at', 'created_at', 'updated_at'
        ]

    def get_artist_name(self, obj):
        artist = getattr(obj.user, 'artist_profile', None)
        return (artist.artistic_name or artist.name) if artist else ''


class AdminSongPromotionSerializer(AdminSignedMediaSerializerMixin, serializers.ModelSerializer):
    song_title = serializers.CharField(source='song.title', read_only=True)
    artist_name = serializers.CharField(source='song.artist.name', read_only=True)
    cover_image = serializers.CharField(source='song.cover_image', read_only=True)
    is_running = serializers.SerializerMethodField()

    class Meta:
        model = SongPromotion
        fields = [
            'id', 'song', 'song_title', 'artist_name', 'cover_image', 'aggression',
            'starts_at', 'ends_at', 'is_active', 'is_running', 'created_by',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at', 'is_running']

    def validate(self, attrs):
        attrs = super().validate(attrs)
        starts_at = attrs.get('starts_at', getattr(self.instance, 'starts_at', None))
        ends_at = attrs.get('ends_at', getattr(self.instance, 'ends_at', None))
        song = attrs.get('song', getattr(self.instance, 'song', None))
        if starts_at and ends_at and ends_at <= starts_at:
            raise serializers.ValidationError({'ends_at': 'زمان پایان باید بعد از زمان شروع باشد.'})
        if song and song.status != Song.STATUS_PUBLISHED:
            raise serializers.ValidationError({'song': 'فقط آهنگ منتشرشده قابل پروموت است.'})
        return attrs

    def get_is_running(self, obj):
        from django.utils import timezone
        now = timezone.now()
        return bool(obj.is_active and obj.starts_at <= now < obj.ends_at)
