from django.contrib import admin
from django.contrib.auth import get_user_model
from .models import Artist, Album, Genre, Mood, Tag, SubGenre, Song, Playlist, UserPlaylist

User = get_user_model()


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('id', 'phone_number', 'roles', 'is_staff', 'is_active', 'date_joined')
    list_filter = ('roles', 'is_staff', 'is_active')
    search_fields = ('phone_number',)


@admin.register(Artist)
class ArtistAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'user', 'verified', 'created_at')
    list_filter = ('verified', 'created_at')
    search_fields = ('name', 'user__phone_number')
    readonly_fields = ('created_at',)
    fieldsets = (
        ('Basic Info', {
            'fields': ('name', 'user', 'bio', 'verified')
        }),
        ('Media', {
            'fields': ('profile_image',)
        }),
        ('Metadata', {
            'fields': ('created_at',)
        }),
    )


@admin.register(Album)
class AlbumAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'artist', 'release_date', 'created_at')
    list_filter = ('release_date', 'created_at', 'artist')
    search_fields = ('title', 'artist__name')
    readonly_fields = ('created_at',)
    autocomplete_fields = ['artist']
    fieldsets = (
        ('Basic Info', {
            'fields': ('title', 'artist', 'release_date')
        }),
        ('Media', {
            'fields': ('cover_image',)
        }),
        ('Description', {
            'fields': ('description',)
        }),
        ('Metadata', {
            'fields': ('created_at',)
        }),
    )


@admin.register(Genre)
class GenreAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'slug')
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Mood)
class MoodAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'slug')
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'slug')
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(SubGenre)
class SubGenreAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'slug', 'parent_genre')
    list_filter = ('parent_genre',)
    search_fields = ('name', 'slug')
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
    search_fields = ('title', 'artist__name', 'description', 'lyrics')
    readonly_fields = ('plays', 'duration_display', 'display_title', 'created_at', 'updated_at')
    autocomplete_fields = ['artist', 'album', 'uploader']
    filter_horizontal = ('genres', 'moods', 'tags')
    
    fieldsets = (
        ('Basic Information', {
            'fields': (
                'title', 'artist', 'featured_artists', 'album', 'is_single', 'display_title'
            )
        }),
        ('Files & Media', {
            'fields': ('audio_file', 'cover_image', 'original_format')
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
            'fields': ('description', 'lyrics'),
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
            'fields': ('label', 'producers', 'composers', 'lyricists', 'credits'),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def display_featured(self, obj):
        """Display featured artists in list view"""
        if obj.featured_artists:
            return ', '.join(obj.featured_artists)
        return '-'
    display_featured.short_description = 'Featured Artists'
    
    actions = ['mark_as_published', 'mark_as_draft', 'mark_as_pending']
    
    def mark_as_published(self, request, queryset):
        """Bulk action to publish songs"""
        count = queryset.update(status=Song.STATUS_PUBLISHED)
        self.message_user(request, f'{count} song(s) marked as published.')
    mark_as_published.short_description = 'Mark selected as Published'
    
    def mark_as_draft(self, request, queryset):
        """Bulk action to mark as draft"""
        count = queryset.update(status=Song.STATUS_DRAFT)
        self.message_user(request, f'{count} song(s) marked as draft.')
    mark_as_draft.short_description = 'Mark selected as Draft'
    
    def mark_as_pending(self, request, queryset):
        """Bulk action to mark as pending"""
        count = queryset.update(status=Song.STATUS_PENDING)
        self.message_user(request, f'{count} song(s) marked as pending review.')
    mark_as_pending.short_description = 'Mark selected as Pending Review'


@admin.register(Playlist)
class PlaylistAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'cover_image', 'created_by', 'created_at')
    list_filter = ('created_by', 'created_at', 'genres', 'moods')
    search_fields = ('title',)
    readonly_fields = ('created_at',)
    filter_horizontal = ('genres', 'moods', 'tags', 'songs')
    fieldsets = (
        ('Basic Info', {'fields': ('title', 'description', 'cover_image', 'created_by')}),
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
    
    def make_public(self, request, queryset):
        """Bulk action to make playlists public"""
        count = queryset.update(public=True)
        self.message_user(request, f'{count} playlist(s) made public.')
    make_public.short_description = 'Make selected playlists public'
    
    def make_private(self, request, queryset):
        """Bulk action to make playlists private"""
        count = queryset.update(public=False)
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


# Add likes information to existing SongAdmin
def song_likes_count(song):
    return song.liked_by.count()
song_likes_count.short_description = 'Likes Count'

def song_liked_users(song):
    users = song.liked_by.all()[:5]  # Show first 5 users
    user_list = [user.phone_number for user in users]
    if song.liked_by.count() > 5:
        user_list.append(f"... and {song.liked_by.count() - 5} more")
    return ", ".join(user_list) if user_list else "No likes"
song_liked_users.short_description = 'Liked By'


# Enhance SongAdmin to show likes
SongAdmin.list_display = SongAdmin.list_display + ('song_likes_count',)
SongAdmin.list_filter = SongAdmin.list_filter + ('liked_by',)
SongAdmin.readonly_fields = SongAdmin.readonly_fields + ('song_likes_count', 'song_liked_users')
SongAdmin.song_likes_count = song_likes_count
SongAdmin.song_liked_users = song_liked_users


# Create a custom admin view for likes using a proxy approach
from django.db import models

class SongLikeProxy(models.Model):
    """Proxy model to display song likes in admin"""
    song = models.ForeignKey(Song, on_delete=models.CASCADE, related_name='like_proxy')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='song_like_proxy')
    
    class Meta:
        app_label = 'api'
        db_table = None  # This is a proxy, no actual table
        managed = False
        verbose_name = 'Song Like'
        verbose_name_plural = 'Song Likes'
    
    def __str__(self):
        return f"{self.user.phone_number} likes {self.song.title}"


class SongLikeAdmin(admin.ModelAdmin):
    """Admin view for displaying all song likes"""
    
    list_display = ('song_title', 'artist_name', 'user_phone', 'user_name', 'song_created')
    list_filter = ('song__artist', 'song__genres', 'song__moods', 'user__roles')
    search_fields = ('song__title', 'song__artist__name', 'user__phone_number', 'user__first_name', 'user__last_name')
    date_hierarchy = 'song__created_at'
    readonly_fields = ('song_title', 'artist_name', 'user_phone', 'user_name', 'song_created', 'song_details')
    
    def get_queryset(self, request):
        """Override to show all likes using a custom query"""
        # Use raw SQL or a custom query to get likes
        # Since likes are ManyToMany, we'll use a subquery approach
        from django.db.models import Exists, OuterRef
        
        # Get songs that have likes
        return Song.objects.filter(
            Exists(User.objects.filter(song_liked_by=OuterRef('pk')))
        ).prefetch_related('liked_by', 'artist', 'genres', 'moods')
    
    def song_title(self, obj):
        return obj.title
    song_title.short_description = 'Song Title'
    song_title.admin_order_field = 'title'
    
    def artist_name(self, obj):
        return obj.artist.name
    artist_name.short_description = 'Artist'
    artist_name.admin_order_field = 'artist__name'
    
    def user_phone(self, obj):
        # This is tricky since we have multiple users per song
        # For display purposes, we'll show the first user
        first_user = obj.liked_by.first()
        return first_user.phone_number if first_user else "N/A"
    user_phone.short_description = 'Sample User Phone'
    
    def user_name(self, obj):
        first_user = obj.liked_by.first()
        if first_user and (first_user.first_name or first_user.last_name):
            return f"{first_user.first_name} {first_user.last_name}".strip()
        return "-"
    user_name.short_description = 'Sample User Name'
    
    def song_created(self, obj):
        return obj.created_at
    song_created.short_description = 'Song Created'
    song_created.admin_order_field = 'created_at'
    
    def song_details(self, obj):
        genres = ", ".join([g.name for g in obj.genres.all()])
        moods = ", ".join([m.name for m in obj.moods.all()])
        likes_count = obj.liked_by.count()
        return f"Genres: {genres}\nMoods: {moods}\nTotal Likes: {likes_count}"
    song_details.short_description = 'Song Details'
    
    def has_add_permission(self, request):
        return False
    
    def has_change_permission(self, request, obj=None):
        return False
    
    def has_delete_permission(self, request, obj=None):
        return False


# Register the proxy admin
admin.site.register(SongLikeProxy, SongLikeAdmin)
