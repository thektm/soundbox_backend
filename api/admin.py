from django.contrib import admin
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.http import HttpResponseRedirect
from .models import (
    Artist, ArtistAuth, Album, Genre, Mood, Tag, SubGenre, Song, Playlist, 
    UserPlaylist, RecommendedPlaylist, EventPlaylist, SearchSection,
    ArtistMonthlyListener, UserHistory, NotificationSetting, Follow, Rules, PlayConfiguration,
    InitialCheck,
    PaymentTransaction, BannerAd, AudioAd, ArtistSocialAccount, SocialPlatform, Report,
    UserImageProfile
)
from .models import OtpCode
from .models import ActivePlayback
from django.utils import timezone
from django.db import transaction

User = get_user_model()


class RequireEnglishTranslationAdminMixin:
    """Require English counterparts for admin-authored translatable text.

    Programmatic/server generation is unaffected. In Django admin, an English
    value is required whenever its Farsi source field contains text.
    """

    translation_pairs = ()

    def get_form(self, request, obj=None, change=False, **kwargs):
        base_form = super().get_form(request, obj, change, **kwargs)
        pairs = tuple(self.translation_pairs)

        class TranslationValidatedForm(base_form):
            def clean(self):
                cleaned = super().clean()
                for source_name, english_name in pairs:
                    source_value = cleaned.get(source_name)
                    english_value = cleaned.get(english_name)
                    if source_value and not str(english_value or "").strip():
                        self.add_error(
                            english_name,
                            "Enter the real English equivalent; transliteration/Finglish is not accepted.",
                        )
                return cleaned

        return TranslationValidatedForm


class ArtistSocialAccountInline(admin.TabularInline):
    model = ArtistSocialAccount
    extra = 0
    fields = ('platform', 'username', 'url')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Follow)
class FollowAdmin(admin.ModelAdmin):
    list_display = ('id', 'get_follower', 'get_followed', 'created_at')
    list_filter = ('created_at',)
    search_fields = (
        'follower_user__phone_number', 'follower_artist__name',
        'followed_user__phone_number', 'followed_artist__name'
    )
    readonly_fields = ('created_at',)

    def get_follower(self, obj):
        return obj.follower_user or obj.follower_artist
    get_follower.short_description = 'Follower'

    def get_followed(self, obj):
        return obj.followed_user or obj.followed_artist
    get_followed.short_description = 'Followed'


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('id', 'phone_number', 'unique_id', 'roles', 'is_staff', 'is_active', 'date_joined')
    list_filter = ('roles', 'is_staff', 'is_active')
    search_fields = ('phone_number', 'unique_id')


@admin.register(Artist)
class ArtistAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'artistic_name', 'unique_id', 'user', 'verified', 'city', 'email', 'id_number', 'created_at')
    list_filter = ('verified', 'created_at', 'city')
    search_fields = ('name', 'name_en', 'artistic_name', 'artistic_name_en', 'unique_id', 'user__phone_number', 'email', 'id_number', 'city', 'city_en')
    readonly_fields = ('created_at',)
    inlines = [ArtistSocialAccountInline]
    fieldsets = (
        ('Basic Info', {
            'fields': ('name', 'name_en', 'artistic_name', 'artistic_name_en', 'unique_id', 'user', 'bio', 'bio_en', 'verified', 'email', 'city', 'city_en', 'id_number', 'date_of_birth', 'address', 'address_en')
        }),
        ('Media', {
            'fields': ('profile_image', 'banner_image')
        }),
        ('Metadata', {
            'fields': ('created_at',)
        }),
    )


@admin.register(ArtistAuth)
class ArtistAuthAdmin(admin.ModelAdmin):
    list_display = ('id', 'stage_name', 'first_name', 'last_name', 'user', 'auth_type', 'status', 'is_verified', 'created_at')
    list_filter = ('auth_type', 'status', 'is_verified', 'created_at')
    search_fields = ('stage_name', 'first_name', 'last_name', 'user__phone_number', 'national_id')
    readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        ('Personal', {'fields': ('user', 'auth_type', 'first_name', 'last_name', 'stage_name', 'birth_date')}),
        ('Contact', {'fields': ('phone_number', 'email', 'city', 'address')}),
        ('Verification', {'fields': ('national_id', 'national_id_image', 'biography', 'status', 'is_verified')}),
        ('Timestamps', {'fields': ('created_at', 'updated_at')})
    )

@admin.register(ArtistSocialAccount)
class ArtistSocialAccountAdmin(admin.ModelAdmin):
    list_display = ('id', 'artist', 'platform', 'username', 'url', 'updated_at')
    list_filter = ('platform', 'updated_at')
    search_fields = ('artist__name', 'platform__name', 'username', 'url')
    readonly_fields = ('created_at', 'updated_at')

@admin.register(SocialPlatform)
class SocialPlatformAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'name_en', 'slug', 'base_url')
    search_fields = ('name', 'name_en', 'slug')
    prepopulated_fields = {'slug': ('name',)}

@admin.register(ArtistMonthlyListener)
class ArtistMonthlyListenerAdmin(admin.ModelAdmin):
    list_display = ('id', 'artist', 'user', 'updated_at')
    list_filter = ('updated_at', 'artist')
    search_fields = ('artist__name', 'user__phone_number')
    readonly_fields = ('updated_at',)


@admin.register(UserHistory)
class UserHistoryAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'content_type', 'updated_at')
    list_filter = ('content_type', 'updated_at')
    search_fields = ('user__phone_number', 'song__title', 'album__title', 'playlist__title', 'artist__name')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(NotificationSetting)
class NotificationSettingAdmin(admin.ModelAdmin):
    list_display = ('user', 'new_song_followed_artists', 'new_album_followed_artists', 'new_playlist', 'new_likes', 'new_follower', 'system_notifications')
    search_fields = ('user__phone_number',)


@admin.register(InitialCheck)
class InitialCheckAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'genres_list', 'created_at', 'updated_at')
    list_filter = ('created_at', 'updated_at')
    search_fields = ('user__phone_number', 'genres__name')
    readonly_fields = ('created_at', 'updated_at')
    filter_horizontal = ('genres',)

    def genres_list(self, obj):
        return ', '.join([g.name for g in obj.genres.all()])
    genres_list.short_description = 'Genres'


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'get_target', 'short_text', 'has_reviewed', 'reviewed_at', 'created_at')
    list_filter = ('has_reviewed', 'created_at')
    search_fields = ('user__phone_number', 'song__title', 'artist__name', 'text')
    readonly_fields = ('created_at', 'updated_at')
    actions = ['mark_reviewed', 'mark_unreviewed']

    def get_target(self, obj):
        if obj.song:
            return f"Song: {obj.song.title} (id={obj.song.id})"
        if obj.artist:
            return f"Artist: {obj.artist.name} (id={obj.artist.id})"
        return 'Unknown'
    get_target.short_description = 'Target'

    def short_text(self, obj):
        return (obj.text[:75] + '...') if len(obj.text or '') > 75 else (obj.text or '')
    short_text.short_description = 'Text'

    def mark_reviewed(self, request, queryset):
        now = timezone.now()
        updated = queryset.update(has_reviewed=True, reviewed_at=now)
        self.message_user(request, f'Marked {updated} report(s) as reviewed.')
    mark_reviewed.short_description = 'Mark selected reports as reviewed'

    def mark_unreviewed(self, request, queryset):
        updated = queryset.update(has_reviewed=False, reviewed_at=None)
        self.message_user(request, f'Marked {updated} report(s) as unreviewed.')
    mark_unreviewed.short_description = 'Mark selected reports as unreviewed'


@admin.register(OtpCode)
class OtpCodeAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'purpose', 'code', 'expires_at', 'attempts', 'consumed', 'created_at')
    list_filter = ('purpose', 'consumed', 'expires_at', 'created_at')
    search_fields = ('user__phone_number', 'code')
    readonly_fields = ('created_at', 'code', 'code_hash')
    raw_id_fields = ('user',)
    ordering = ('-created_at',)


@admin.register(UserImageProfile)
class UserImageProfileAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('user__phone_number', 'user__unique_id')
    readonly_fields = ('created_at', 'updated_at')
    raw_id_fields = ('user',)


@admin.register(Album)
class AlbumAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'artist', 'release_date', 'created_at')
    list_filter = ('release_date', 'created_at', 'artist')
    search_fields = ('title', 'title_en', 'description', 'description_en', 'artist__name', 'artist__name_en')
    readonly_fields = ('created_at',)
    autocomplete_fields = ['artist']
    fieldsets = (
        ('Basic Info', {
            'fields': ('title', 'title_en', 'artist', 'release_date')
        }),
        ('Media', {
            'fields': ('cover_image',)
        }),
        ('Description', {
            'fields': ('description', 'description_en')
        }),
        ('Metadata', {
            'fields': ('created_at',)
        }),
    )


@admin.register(Genre)
class GenreAdmin(RequireEnglishTranslationAdminMixin, admin.ModelAdmin):
    translation_pairs = (('name', 'name_en'),)
    list_display = ('id', 'name', 'name_en', 'slug')
    search_fields = ('name', 'name_en', 'slug')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Mood)
class MoodAdmin(RequireEnglishTranslationAdminMixin, admin.ModelAdmin):
    translation_pairs = (('name', 'name_en'),)
    list_display = ('id', 'name', 'name_en', 'slug')
    search_fields = ('name', 'name_en', 'slug')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Tag)
class TagAdmin(RequireEnglishTranslationAdminMixin, admin.ModelAdmin):
    translation_pairs = (('name', 'name_en'),)
    list_display = ('id', 'name', 'name_en', 'slug')
    search_fields = ('name', 'name_en', 'slug')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(SubGenre)
class SubGenreAdmin(RequireEnglishTranslationAdminMixin, admin.ModelAdmin):
    translation_pairs = (('name', 'name_en'),)
    list_display = ('id', 'name', 'slug', 'parent_genre')
    list_filter = ('parent_genre',)
    search_fields = ('name', 'name_en', 'slug')
    prepopulated_fields = {'slug': ('name',)}
    autocomplete_fields = ['parent_genre']


@admin.register(Song)
class SongAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'title', 'artist', 'display_featured', 'album', 'status', 
        'plays', 'duration_display', 'release_date', 'created_at'
    )
    list_filter = (
        'status', 'is_single', 'language', 'live_performed', 
        'release_date', 'created_at', 'artist', 'genres', 'moods'
    )
    # Add date hierarchy for quick drill-down by the record creation date
    date_hierarchy = 'created_at'
    search_fields = ('title', 'title_en', 'artist__name', 'artist__name_en', 'description', 'description_en', 'lyrics', 'lyrics_en', 'label', 'label_en')
    readonly_fields = ('plays', 'duration_display', 'display_title', 'created_at', 'updated_at')
    autocomplete_fields = ['artist', 'album', 'uploader']
    filter_horizontal = ('genres', 'moods', 'tags', 'featured_artists')
    
    fieldsets = (
        ('Basic Information', {
            'fields': (
                'title', 'title_en', 'artist', 'featured_artists', 'album', 'is_single', 'display_title'
            )
        }),
        ('Files & Media', {
            'fields': ('audio_file','converted_audio_url', 'cover_image', 'original_format')
        }),
        ('Playback Information', {
            'fields': ('duration_seconds', 'duration_display', 'plays')
        }),
        ('Status & Moderation', {
            'fields': ('status', 'uploader')
        }),
        ('Release & Language', {
            'fields': ('release_date', 'language')
        }),
        ('Classification', {
            'fields': ('genres', 'sub_genres', 'moods', 'tags'),
            'classes': ('collapse',)
        }),
        ('Description & Lyrics', {
            'fields': ('description', 'description_en', 'lyrics', 'lyrics_en'),
            'classes': ('collapse',)
        }),
        ('Audio Features', {
            'fields': (
                'tempo', 'energy', 'danceability', 'valence', 
                'acousticness', 'instrumentalness', 'speechiness', 'live_performed'
            ),
            'classes': ('collapse',)
        }),
        ('Credits & Legal', {
            'fields': ('label', 'label_en', 'producers', 'producers_en', 'composers', 'composers_en', 'lyricists', 'lyricists_en', 'credits', 'credits_en'),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def display_featured(self, obj):
        """Display featured artists in list view"""
        featured_names = [a.name for a in obj.featured_artists.all()]
        if featured_names:
            return ', '.join(featured_names)
        return '-'
    display_featured.short_description = 'Featured Artists'
    
    actions = ['mark_as_published', 'mark_as_draft', 'mark_as_pending']
    
    @staticmethod
    def _bulk_change_status(queryset, new_status):
        # QuerySet.update() bypasses pre_save/post_save and therefore bypasses
        # release, owner-status, cache, and notification hooks. Save each changed
        # song inside one transaction so every normal lifecycle hook is preserved.
        changed = 0
        with transaction.atomic():
            for song in queryset.select_for_update().only('id', 'status'):
                if song.status == new_status:
                    continue
                song.status = new_status
                song.save(update_fields=['status'])
                changed += 1
        return changed

    def mark_as_published(self, request, queryset):
        """Bulk action to publish songs without bypassing lifecycle hooks."""
        count = self._bulk_change_status(queryset, Song.STATUS_PUBLISHED)
        self.message_user(request, f'{count} song(s) marked as published.')
    mark_as_published.short_description = 'Mark selected as Published'
    
    def mark_as_draft(self, request, queryset):
        """Bulk action to mark as draft without bypassing lifecycle hooks."""
        count = self._bulk_change_status(queryset, Song.STATUS_DRAFT)
        self.message_user(request, f'{count} song(s) marked as draft.')
    mark_as_draft.short_description = 'Mark selected as Draft'
    
    def mark_as_pending(self, request, queryset):
        """Bulk action to mark as pending without bypassing lifecycle hooks."""
        count = self._bulk_change_status(queryset, Song.STATUS_PENDING)
        self.message_user(request, f'{count} song(s) marked as pending review.')
    mark_as_pending.short_description = 'Mark selected as Pending Review'


@admin.register(Playlist)
class PlaylistAdmin(RequireEnglishTranslationAdminMixin, admin.ModelAdmin):
    translation_pairs = (('title', 'title_en'), ('description', 'description_en'))
    list_display = ('id', 'title', 'cover_image', 'created_by', 'created_at')
    list_filter = ('created_by', 'created_at', 'genres', 'moods')
    search_fields = ('title', 'title_en', 'description', 'description_en')
    readonly_fields = ('created_at',)
    filter_horizontal = ('genres', 'moods', 'tags', 'songs')
    fieldsets = (
        ('Basic Info', {'fields': ('title', 'title_en', 'description', 'description_en', 'cover_image', 'created_by')}),
        ('Classification', {'fields': ('genres', 'moods', 'tags'), 'classes': ('collapse',)}),
        ('Songs', {'fields': ('songs',)}),
        ('Metadata', {'fields': ('created_at',)}),
    )


@admin.register(UserPlaylist)
class UserPlaylistAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'user', 'public', 'songs_count', 'likes_count', 'created_at')
    list_filter = ('public', 'created_at', 'updated_at', 'user')
    search_fields = ('title', 'user__phone_number', 'user__first_name', 'user__last_name')
    readonly_fields = ('created_at', 'updated_at', 'songs_count', 'likes_count')
    filter_horizontal = ('liked_by', 'songs')
    autocomplete_fields = ['user']
    date_hierarchy = 'created_at'
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('user', 'title', 'public')
        }),
        ('Content', {
            'fields': ('songs',),
            'description': 'Select songs to include in this playlist'
        }),
        ('Social Features', {
            'fields': ('liked_by',),
            'classes': ('collapse',),
            'description': 'Users who have liked this playlist'
        }),
        ('Statistics', {
            'fields': ('songs_count', 'likes_count'),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def songs_count(self, obj):
        """Display the number of songs in the playlist"""
        return obj.songs.count()
    songs_count.short_description = 'Songs Count'
    
    def likes_count(self, obj):
        """Display the number of likes for the playlist"""
        return obj.liked_by.count()
    likes_count.short_description = 'Likes Count'
    
    actions = ['make_public', 'make_private', 'clear_likes']
    
    @staticmethod
    def _bulk_change_visibility(queryset, is_public):
        # Preserve the public-playlist notification transition signal.
        changed = 0
        with transaction.atomic():
            for playlist in queryset.select_for_update().only('id', 'public'):
                if playlist.public == is_public:
                    continue
                playlist.public = is_public
                playlist.save(update_fields=['public', 'updated_at'])
                changed += 1
        return changed

    def make_public(self, request, queryset):
        """Bulk action to make playlists public without bypassing hooks."""
        count = self._bulk_change_visibility(queryset, True)
        self.message_user(request, f'{count} playlist(s) made public.')
    make_public.short_description = 'Make selected playlists public'
    
    def make_private(self, request, queryset):
        """Bulk action to make playlists private without bypassing hooks."""
        count = self._bulk_change_visibility(queryset, False)
        self.message_user(request, f'{count} playlist(s) made private.')
    make_private.short_description = 'Make selected playlists private'
    
    def clear_likes(self, request, queryset):
        """Bulk action to clear all likes from playlists"""
        count = 0
        for playlist in queryset:
            playlist.liked_by.clear()
            count += 1
        self.message_user(request, f'Likes cleared from {count} playlist(s).')
    clear_likes.short_description = 'Clear likes from selected playlists'


# Register auto-generated through models for likes so they appear as separate tables
# This avoids changing existing models and exposes the implicit M2M join tables
SongLike = Song.liked_by.through
# Give the auto through-model a friendly name in the admin
try:
    SongLike._meta.verbose_name = 'like'
    SongLike._meta.verbose_name_plural = 'likes'
except Exception:
    pass

@admin.register(SongLike)
class SongLikeAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'song')
    list_filter = ('song',)
    search_fields = ('user__phone_number', 'song__title')
    raw_id_fields = ('user', 'song')
    ordering = ('-id',)


PlaylistLike = UserPlaylist.liked_by.through
# Friendly admin name for playlist likes
try:
    PlaylistLike._meta.verbose_name = 'playlist like'
    PlaylistLike._meta.verbose_name_plural = 'playlist likes'
except Exception:
    pass

@admin.register(PlaylistLike)
class PlaylistLikeAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'userplaylist')
    list_filter = ('userplaylist',)
    search_fields = ('user__phone_number', 'userplaylist__title')
    raw_id_fields = ('user', 'userplaylist')
    ordering = ('-id',)


@admin.register(RecommendedPlaylist)
class RecommendedPlaylistAdmin(RequireEnglishTranslationAdminMixin, admin.ModelAdmin):
    translation_pairs = (('title', 'title_en'), ('description', 'description_en'))
    list_display = ('id', 'title', 'playlist_type', 'user', 'match_percentage', 'relevance_score', 'views', 'created_at')
    list_filter = ('playlist_type', 'created_at', 'user')
    search_fields = ('title', 'title_en', 'description', 'description_en', 'unique_id', 'user__phone_number')
    readonly_fields = ('created_at', 'updated_at', 'views')
    filter_horizontal = ('songs', 'liked_by', 'saved_by', 'viewed_by')
    readonly_fields = ('created_at', 'updated_at', 'views', 'song_order')
    fieldsets = (
        ('Basic Info', {
            'fields': ('unique_id', 'title', 'title_en', 'description', 'description_en', 'playlist_type', 'user')
        }),
        ('Songs', {
            'fields': ('songs', 'song_order')
        }),
        ('Metrics', {
            'fields': ('relevance_score', 'match_percentage', 'views', 'expires_at')
        }),
        ('User Interactions', {
            'fields': ('liked_by', 'saved_by', 'viewed_by')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )


@admin.register(EventPlaylist)
class EventPlaylistAdmin(RequireEnglishTranslationAdminMixin, admin.ModelAdmin):
    translation_pairs = (('title', 'title_en'),)
    list_display = ('id', 'title', 'time_of_day', 'playlists_count', 'created_at')
    list_filter = ('time_of_day', 'created_at')
    search_fields = ('title', 'title_en')
    autocomplete_fields = ['playlists']
    readonly_fields = ('created_at', 'updated_at')
    
    fieldsets = (
        ('Basic Info', {
            'fields': ('title', 'title_en', 'time_of_day')
        }),
        ('Playlists', {
            'fields': ('playlists',),
            'description': 'Select playlists to include in this event group. You can create new playlists using the plus icon.'
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def playlists_count(self, obj):
        return obj.playlists.count()
    playlists_count.short_description = 'Playlists Count'


@admin.register(SearchSection)
class SearchSectionAdmin(RequireEnglishTranslationAdminMixin, admin.ModelAdmin):
    translation_pairs = (('title', 'title_en'),)
    list_display = ('id', 'title', 'type', 'item_size', 'created_at', 'created_by')
    list_filter = ('type', 'item_size', 'created_at')
    search_fields = ('title', 'title_en')
    readonly_fields = ('created_at', 'updated_at', 'created_by', 'updated_by')
    filter_horizontal = ('songs', 'albums', 'playlists')
    
    fieldsets = (
        ('Basic Info', {
            'fields': ('title', 'title_en', 'type', 'item_size', 'icon_logo')
        }),
        ('Content Items', {
            'fields': ('songs', 'albums', 'playlists'),
            'description': 'Select items based on the section type. Only the relevant items will be used in the API.'
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at', 'created_by', 'updated_by'),
            'classes': ('collapse',)
        }),
    )

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(Rules)
class RulesAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'version', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('title', 'title_en', 'content', 'content_en', 'version')
    readonly_fields = ('version', 'created_at')
    fieldsets = (
        ('Basic Info', {
            'fields': ('title', 'title_en', 'content', 'content_en')
        }),
        ('Versioning', {
            'fields': ('version', 'created_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(PlayConfiguration)
class PlayConfigurationAdmin(admin.ModelAdmin):
    list_display = ('free_play_worth', 'premium_play_worth', 'updated_at')
    readonly_fields = ('updated_at',)


@admin.register(ActivePlayback)
class ActivePlaybackAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'song', 'start_time', 'expiration_time')
    list_filter = ('start_time', 'expiration_time')
    search_fields = ('user__phone_number', 'song__title')
    readonly_fields = ('start_time',)


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'transaction_id', 'amount', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('user__phone_number', 'transaction_id', 'description', 'description_en')
    readonly_fields = ('created_at',)


@admin.register(BannerAd)
class BannerAdAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'title_en', 'is_active', 'created_at')
    list_filter = ('is_active', 'created_at')
    search_fields = ('title', 'title_en', 'navigate_link')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(AudioAd)
class AudioAdAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'title_en', 'duration', 'skippable_after', 'is_active', 'created_at')
    list_filter = ('is_active', 'created_at')
    search_fields = ('title', 'title_en', 'navigate_link')
    readonly_fields = ('created_at', 'updated_at')

