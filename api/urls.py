# Base URL is : https://api.sedabox.com/
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    UserProfileView,
    NotificationSettingUpdateView,
    StreamQualityUpdateView,
    UserFollowView,
    LikedSongsView,
    LikedAlbumsView,
    LikedPlaylistsView,
    MyArtistsView,
    MyLibraryView,
    R2UploadView,
    SongUploadView,
    ArtistListView,
    ArtistDetailView,
    ArtistHomeView,
    ArtistAnalyticsView,
    ArtistLiveListenersView,
    ArtistLiveListenersPollView,
    ArtistSongsManagementView,
    ArtistAlbumsManagementView,
    DepositRequestView,
    ArtistWalletView,
    AlbumListView,
    AlbumDetailView,
    AlbumLikeView,
    GenreListView,
    GenreDetailView,
    MoodListView,
    MoodDetailView,
    TagListView,
    TagDetailView,
    SubGenreListView,
    SubGenreDetailView,
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
    PlaylistDetailView,
    PlaylistRecommendationLikeView,
    PlaylistRecommendationSaveView,
    PlaylistSaveToggleView,
    SearchView,
    EventPlaylistView,
    SearchSectionListView,
    SearchSectionDetailView,
    PlaylistLikeView,
    RulesListCreateView,
    RulesDetailView,
    PlayConfigurationView,
)
from .admin_views import (
    AdminUserListView,
    AdminUserDetailView,
    AdminArtistListView,
    AdminArtistDetailView,
    AdminPendingArtistListView,
    AdminPendingArtistDetailView,
)
from .auth_views import (
    AuthRegisterView,
    AuthVerifyView,
    LoginPasswordView,
    LoginOtpRequestView,
    LoginOtpVerifyView,
    ArtistAuthView,
    ForgotPasswordView,
    PasswordResetView,
    ChangePasswordView,
    TokenRefreshView as LocalTokenRefreshView,
    LogoutView,
    SessionListView,
    SessionRevokeView,
    SessionRevokeOtherView,
)
from rest_framework_simplejwt.views import TokenRefreshView

# Create router for remaining viewsets (if any)
router = DefaultRouter()

urlpatterns = [
    # --- Authentication Endpoints (Shared by User and Artist Apps) ---
    path('auth/register/', AuthRegisterView.as_view(), name='auth_register'),
    path('auth/verify/', AuthVerifyView.as_view(), name='auth_verify'),
    path('auth/login/password/', LoginPasswordView.as_view(), name='auth_login_password'),
    path('auth/login/otp/request/', LoginOtpRequestView.as_view(), name='auth_login_otp_request'),
    path('auth/login/otp/verify/', LoginOtpVerifyView.as_view(), name='auth_login_otp_verify'),
    path('auth/password/forgot/', ForgotPasswordView.as_view(), name='auth_password_forgot'),
    path('auth/password/reset/', PasswordResetView.as_view(), name='auth_password_reset'),
    path('auth/password/change/', ChangePasswordView.as_view(), name='auth_password_change'),
    path('auth/token/refresh/', LocalTokenRefreshView.as_view(), name='auth_token_refresh'),
    path('auth/logout/', LogoutView.as_view(), name='auth_logout'),
    path('auth/sessions/', SessionListView.as_view(), name='session_list'),
    path('auth/sessions/<int:pk>/revoke/', SessionRevokeView.as_view(), name='session_revoke'),
    path('auth/sessions/revoke-others/', SessionRevokeOtherView.as_view(), name='session_revoke_others'),
    
    # --- User & Profile Endpoints ---
    path('users/profile/', UserProfileView.as_view(), name='user_profile'),
    path('users/settings/notifications/', NotificationSettingUpdateView.as_view(), name='user_notification_settings'),
    path('users/settings/stream-quality/', StreamQualityUpdateView.as_view(), name='user_stream_quality_settings'),
    path('users/follow/', UserFollowView.as_view(), name='user_follow'),
    path('users/liked-songs/', LikedSongsView.as_view(), name='liked_songs'),
    path('users/liked-albums/', LikedAlbumsView.as_view(), name='liked_albums'),
    path('users/liked-playlists/', LikedPlaylistsView.as_view(), name='liked_playlists'),
    path('users/my-artists/', MyArtistsView.as_view(), name='my_artists'),
    path('users/my-library/', MyLibraryView.as_view(), name='my_library'),
    path('users/songs/recommendations/', UserRecommendationView.as_view(), name='user_recommendations'),
    path('users/latest-releases/', LatestReleasesView.as_view(), name='user_latest_releases'),
    path('users/popular-artists/', PopularArtistsView.as_view(), name='user_popular_artists'),
    path('users/popular-albums/', PopularAlbumsView.as_view(), name='user_popular_albums'),
    
    # --- Global Charts & Top Lists ---
    path('users/weekly-top-songs-global/', WeeklyTopSongsView.as_view(), name='user_weekly_top_songs_global'),
    path('users/weekly-top-artists-global/', WeeklyTopArtistsView.as_view(), name='user_weekly_top_artists_global'),
    path('users/weekly-top-albums-global/', WeeklyTopAlbumsView.as_view(), name='user_weekly_top_albums_global'),
    path('users/daily-top-songs-global/', DailyTopSongsView.as_view(), name='user_daily_top_songs_global'),
    path('users/daily-top-artists-global/', DailyTopArtistsView.as_view(), name='user_daily_top_artists_global'),
    path('users/daily-top-albums-global/', DailyTopAlbumsView.as_view(), name='user_daily_top_albums_global'),
    
    # --- Recommendations & Discovery ---
    path('users/playlist-recommendations/', PlaylistRecommendationsView.as_view(), name='user_playlist_recommendations'),
    path('users/playlist-recommendations/<str:unique_id>/', PlaylistRecommendationDetailView.as_view(), name='user_playlist_recommendation_detail'),
    path('users/playlist-recommendations/<str:unique_id>/like/', PlaylistRecommendationLikeView.as_view(), name='user_playlist_recommendation_like'),
    path('users/playlist-recommendations/<str:unique_id>/save/', PlaylistRecommendationSaveView.as_view(), name='user_playlist_recommendation_save'),
    
    # --- Artist App Endpoints ---
    path('artist/home/', ArtistHomeView.as_view(), name='artist_home'),
    path('artist/analytics/', ArtistAnalyticsView.as_view(), name='artist_analytics'),
    path('artist/live-listeners/', ArtistLiveListenersView.as_view(), name='artist_live_listeners'),
    path('artist/live-listeners/poll/', ArtistLiveListenersPollView.as_view(), name='artist_live_listeners_poll'),
    path('artist/songs-management/', ArtistSongsManagementView.as_view(), name='artist_songs_management'),
    path('artist/songs/upload/', ArtistSongsManagementView.as_view(), name='artist_songs_upload'),
    path('artist/songs/<int:pk>/', ArtistSongsManagementView.as_view(), name='artist_songs_detail'),
    path('artist/albums/', ArtistAlbumsManagementView.as_view(), name='artist_albums_management'),
    path('artist/albums/<int:pk>/', ArtistAlbumsManagementView.as_view(), name='artist_albums_detail'),
    path('artist/deposit-request/', DepositRequestView.as_view(), name='artist_deposit_request'),
    path('artist/wallet/', ArtistWalletView.as_view(), name='artist_wallet'),
    path('artist/finance/', __import__('api.views', fromlist=['ArtistFinanceView']).ArtistFinanceView.as_view(), name='artist_finance'),
    path('artist/finance/songs/', __import__('api.views', fromlist=['ArtistFinanceSongsView']).ArtistFinanceSongsView.as_view(), name='artist_finance_songs'),
    path('artist/auth/', ArtistAuthView.as_view(), name='artist_auth'),
    path('artist/settings/', __import__('api.views', fromlist=['ArtistSettingsView']).ArtistSettingsView.as_view(), name='artist_settings'),
    path('artist/settings/password/', __import__('api.views', fromlist=['ArtistChangePasswordView']).ArtistChangePasswordView.as_view(), name='artist_change_password'),
    path('songs/upload/', SongUploadView.as_view(), name='song_upload'),

    # --- Artist Discovery Endpoints ---
    path('artists/', ArtistListView.as_view(), name='artist_list'),
    path('artists/<int:pk>/', ArtistDetailView.as_view(), name='artist_detail'),

    # --- Album Endpoints ---
    path('albums/', AlbumListView.as_view(), name='album_list'),
    path('albums/<int:pk>/', AlbumDetailView.as_view(), name='album_detail'),
    path('albums/<int:pk>/like/', AlbumLikeView.as_view(), name='album_like'),

    # --- Genre & SubGenre Endpoints ---
    path('genres/', GenreListView.as_view(), name='genre_list'),
    path('genres/<int:pk>/', GenreDetailView.as_view(), name='genre_detail'),
    path('subgenres/', SubGenreListView.as_view(), name='subgenre_list'),
    path('subgenres/<int:pk>/', SubGenreDetailView.as_view(), name='subgenre_detail'),

    # --- Mood & Tag Endpoints ---
    path('moods/', MoodListView.as_view(), name='mood_list'),
    path('moods/<int:pk>/', MoodDetailView.as_view(), name='mood_detail'),
    path('tags/', TagListView.as_view(), name='tag_list'),
    path('tags/<int:pk>/', TagDetailView.as_view(), name='tag_detail'),

    # --- Song Endpoints ---
    path('songs/', SongListView.as_view(), name='song_list'),
    path('songs/<int:pk>/', SongDetailView.as_view(), name='song_detail'),
    path('songs/<int:pk>/like/', SongLikeView.as_view(), name='song_like'),
    
    # --- Media Upload Endpoints ---
    path('upload/', R2UploadView.as_view(), name='r2_upload'),
    
    # --- Streaming & Playback Endpoints ---
    path('songs/stream/', SongStreamListView.as_view(), name='song_stream_list'),
    path('stream/unwrap/<str:token>/', UnwrapStreamView.as_view(), name='unwrap-stream'),
    path('stream/s/<str:token>/', StreamShortRedirectView.as_view(), name='stream-short'),
    path('stream/access/<str:token>/',
        __import__('api.views', fromlist=['StreamAccessView']).StreamAccessView.as_view(),
        name='stream-access'),
    
    # --- Analytics & Search ---
    path('play/count/', PlayCountView.as_view(), name='play_count'),
    path('play/configuration/', PlayConfigurationView.as_view(), name='play_configuration'),
    path('search/', SearchView.as_view(), name='search'),
    
    # --- Curated Content Endpoints ---
    path('event-playlists/', EventPlaylistView.as_view(), name='event_playlist_list'),
    path('playlists/<int:pk>/', PlaylistDetailView.as_view(), name='playlist_detail'),
    path('playlists/<int:pk>/like/', PlaylistLikeView.as_view(), name='playlist_like'),
    path('playlists/<int:pk>/save/', PlaylistSaveToggleView.as_view(), name='playlist_save_toggle'),
    path('search-sections/', SearchSectionListView.as_view(), name='search_section_list'),
    path('search-sections/<int:pk>/', SearchSectionDetailView.as_view(), name='search_section_detail'),
    
    # --- User Playlist Endpoints ---
    path('user-playlists/', UserPlaylistListCreateView.as_view(), name='user_playlist_list_create'),
    path('user-playlists/<int:pk>/', UserPlaylistDetailView.as_view(), name='user_playlist_detail'),
    path('user-playlists/<int:pk>/add-song/', UserPlaylistAddSongView.as_view(), name='user_playlist_add_song'),
    path('user-playlists/<int:pk>/remove-song/<int:song_id>/', UserPlaylistRemoveSongView.as_view(), name='user_playlist_remove_song'),
    
    # --- Rules Endpoints ---
    path('rules/', RulesListCreateView.as_view(), name='rules_list_create'),
    path('rules/latest/', __import__('api.views', fromlist=['RulesLatestView']).RulesLatestView.as_view(), name='rules_latest'),
    path('rules/<int:pk>/', RulesDetailView.as_view(), name='rules_detail'),
    
    # --- Admin Endpoints ---
    path('admin/users/', AdminUserListView.as_view(), name='admin_user_list'),
    path('admin/users/<int:pk>/', AdminUserDetailView.as_view(), name='admin_user_detail'),
    path('admin/artists/', AdminArtistListView.as_view(), name='admin_artist_list'),
    path('admin/artists/<int:pk>/', AdminArtistDetailView.as_view(), name='admin_artist_detail'),
    path('admin/pend_artists/', AdminPendingArtistListView.as_view(), name='admin_pending_artist_list'),
    path('admin/pend_artists/<int:pk>/', AdminPendingArtistDetailView.as_view(), name='admin_pending_artist_detail'),

    # Include router URLs (if any)
    path('', include(router.urls)),
]
