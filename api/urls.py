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
    SongViewSet,
    SongStreamListView,
    UnwrapStreamView,
    StreamShortRedirectView,
    PlayCountView,
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
router.register(r'songs', SongViewSet, basename='song')

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
    path('play_count/', PlayCountView.as_view(), name='play_count'),
    
    # Include router URLs
    path('', include(router.urls)),
]
