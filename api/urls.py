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
    # Auth endpoints
    path('register/', RegisterView.as_view(), name='register'),
    path('login/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('refresh/', CustomTokenRefreshView.as_view(), name='token_refresh'),
    
    # User endpoints
    path('users/profile/', UserProfileView.as_view(), name='user_profile'),
    
    # Upload endpoints
    path('upload/', R2UploadView.as_view(), name='r2_upload'),
    path('songs/upload/', SongUploadView.as_view(), name='song_upload'),
    
    # Include router URLs
    path('', include(router.urls)),
]
