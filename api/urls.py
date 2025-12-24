# Base URL is : https://api.sedabox.com/
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    RegisterView,
    CustomTokenObtainPairView,
    CustomTokenRefreshView,
    UserProfileView,
    R2UploadView,
    SongUploadView,
    ArtistViewSet,
    AlbumViewSet,
    GenreViewSet,
    MoodViewSet,
    TagViewSet,
    SubGenreViewSet,
    SongListView,
    SongDetailView,
    SongLikeView,
    SongIncrementPlaysView,
    SongStreamListView,
    UnwrapStreamView,
    StreamShortRedirectView,
    PlayCountView,
    UserPlaylistListCreateView,
    UserPlaylistDetailView,
    UserPlaylistAddSongView,
    UserPlaylistRemoveSongView,
    UserRecommendationView,
    LatestReleasesView,
    PopularArtistsView,
    PopularAlbumsView,
    WeeklyTopSongsView,
    WeeklyTopArtistsView,
    WeeklyTopAlbumsView,
    DailyTopSongsView,
    DailyTopArtistsView,
    DailyTopAlbumsView,
    PlaylistRecommendationsView,
    PlaylistRecommendationDetailView,
    PlaylistRecommendationLikeView,
    PlaylistRecommendationSaveView,
    SearchView,
    EventPlaylistView,
    SearchSectionViewSet,
)
from .auth_views import (
    AuthRegisterView,
    AuthVerifyView,
    LoginPasswordView,
    LoginOtpRequestView,
    LoginOtpVerifyView,
    ForgotPasswordView,
    PasswordResetView,
    TokenRefreshView as LocalTokenRefreshView,
    LogoutView,
)
from rest_framework_simplejwt.views import TokenRefreshView

# Create router for viewsets
router = DefaultRouter()
router.register(r'artists', ArtistViewSet, basename='artist')
router.register(r'albums', AlbumViewSet, basename='album')
router.register(r'genres', GenreViewSet, basename='genre')
router.register(r'moods', MoodViewSet, basename='mood')
router.register(r'tags', TagViewSet, basename='tag')
router.register(r'subgenres', SubGenreViewSet, basename='subgenre')
router.register(r'search-sections', SearchSectionViewSet, basename='search-section')

urlpatterns = [
    

    #auth endpoints
    path('auth/register/', AuthRegisterView.as_view(), name='auth_register'),
    path('auth/verify/', AuthVerifyView.as_view(), name='auth_verify'),
    path('auth/login/password/', LoginPasswordView.as_view(), name='auth_login_password'),
    path('auth/login/otp/request/', LoginOtpRequestView.as_view(), name='auth_login_otp_request'),
    path('auth/login/otp/verify/', LoginOtpVerifyView.as_view(), name='auth_login_otp_verify'),
    path('auth/password/forgot/', ForgotPasswordView.as_view(), name='auth_password_forgot'),
    path('auth/password/reset/', PasswordResetView.as_view(), name='auth_password_reset'),
    path('auth/token/refresh/', LocalTokenRefreshView.as_view(), name='auth_token_refresh'),
    path('auth/logout/', LogoutView.as_view(), name='auth_logout'),
    
    # User endpoints
    path('users/profile/', UserProfileView.as_view(), name='user_profile'),
    path('users/songs/recommendations/', UserRecommendationView.as_view(), name='user_recommendations'),
    # Latest releases (newest first) - paginated with `next` link
    path('users/latest-releases/', LatestReleasesView.as_view(), name='user_latest_releases'),
    # Popular artists ordered by plays + likes + playlist adds
    path('users/popular-artists/', PopularArtistsView.as_view(), name='user_popular_artists'),
    # Popular albums based on song plays and likes + album likes
    path('users/popular-albums/', PopularAlbumsView.as_view(), name='user_popular_albums'),
    # Weekly Top Songs Global
    path('users/weekly-top-songs-global/', WeeklyTopSongsView.as_view(), name='user_weekly_top_songs_global'),
    # Weekly Top Artists Global
    path('users/weekly-top-artists-global/', WeeklyTopArtistsView.as_view(), name='user_weekly_top_artists_global'),
    # Weekly Top Albums Global
    path('users/weekly-top-albums-global/', WeeklyTopAlbumsView.as_view(), name='user_weekly_top_albums_global'),
    # Daily Top Songs Global
    path('users/daily-top-songs-global/', DailyTopSongsView.as_view(), name='user_daily_top_songs_global'),
    # Daily Top Artists Global
    path('users/daily-top-artists-global/', DailyTopArtistsView.as_view(), name='user_daily_top_artists_global'),
    # Daily Top Albums Global
    path('users/daily-top-albums-global/', DailyTopAlbumsView.as_view(), name='user_daily_top_albums_global'),
    # Playlist recommendations - auto-generated based on user activity
    path('users/playlist-recommendations/', PlaylistRecommendationsView.as_view(), name='user_playlist_recommendations'),
    path('users/playlist-recommendations/<str:unique_id>/', PlaylistRecommendationDetailView.as_view(), name='user_playlist_recommendation_detail'),
    path('users/playlist-recommendations/<str:unique_id>/like/', PlaylistRecommendationLikeView.as_view(), name='user_playlist_recommendation_like'),
    path('users/playlist-recommendations/<str:unique_id>/save/', PlaylistRecommendationSaveView.as_view(), name='user_playlist_recommendation_save'),
    
    # Song endpoints
    path('songs/', SongListView.as_view(), name='song_list'),
    path('songs/<int:pk>/', SongDetailView.as_view(), name='song_detail'),
    path('songs/<int:pk>/like/', SongLikeView.as_view(), name='song_like'),
    path('songs/<int:pk>/increment_plays/', SongIncrementPlaysView.as_view(), name='song_increment_plays'),
    
    # Upload endpoints
    path('upload/', R2UploadView.as_view(), name='r2_upload'),
    path('songs/upload/', SongUploadView.as_view(), name='song_upload'),
    
    # Stream endpoints
    path('songs/stream/', SongStreamListView.as_view(), name='song_stream_list'),
    path('stream/unwrap/<str:token>/', UnwrapStreamView.as_view(), name='unwrap-stream'),
        path('stream/s/<str:token>/', StreamShortRedirectView.as_view(), name='stream-short'),
        path('stream/access/<str:token>/',
            # one-time access endpoint (redirects once to presigned R2 URL)
            __import__('api.views', fromlist=['StreamAccessView']).StreamAccessView.as_view(),
            name='stream-access'),
    
    # Play count endpoint
    path('play/count/', PlayCountView.as_view(), name='play_count'),
    
    # Search endpoint
    path('search/', SearchView.as_view(), name='search'),
    
    # Event Playlists
    path('event-playlists/', EventPlaylistView.as_view(), name='event_playlist_list'),
    
    # User Playlist endpoints
    path('user-playlists/', UserPlaylistListCreateView.as_view(), name='user_playlist_list_create'),
    path('user-playlists/<int:pk>/', UserPlaylistDetailView.as_view(), name='user_playlist_detail'),
    path('user-playlists/<int:pk>/add-song/', UserPlaylistAddSongView.as_view(), name='user_playlist_add_song'),
    path('user-playlists/<int:pk>/remove-song/<int:song_id>/', UserPlaylistRemoveSongView.as_view(), name='user_playlist_remove_song'),
    
    # Include router URLs
    path('', include(router.urls)),
]
