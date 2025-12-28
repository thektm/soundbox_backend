from rest_framework import generics, permissions, viewsets
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.decorators import action
from .models import (
    User, Artist, Album, Playlist,NotificationSetting, Genre, Mood, Tag, SubGenre, Song, 
    StreamAccess, PlayCount, UserPlaylist, RecommendedPlaylist, EventPlaylist, SearchSection,
    ArtistMonthlyListener, UserHistory, Follow, SongLike, AlbumLike, PlaylistLike, Rules, PlayConfiguration
)
from .serializers import (
    UserSerializer,PlaylistSerializer,NotificationSettingSerializer,
    RegisterSerializer, 
    CustomTokenObtainPairSerializer,
    ArtistSerializer,
    PopularArtistSerializer,
    AlbumSerializer,
    PopularAlbumSerializer,
    GenreSerializer,
    MoodSerializer,
    TagSerializer,
    SubGenreSerializer,
    SongSerializer,
    SongUploadSerializer,
    UploadSerializer,
    SongStreamSerializer,
    UserPlaylistSerializer,
    UserPlaylistCreateSerializer,
    RecommendedPlaylistListSerializer,
    RecommendedPlaylistDetailSerializer,
    SearchResultSerializer,
    EventPlaylistSerializer,
    SearchSectionSerializer,
    FollowRequestSerializer,
    LikedSongSerializer,
    LikedAlbumSerializer,
    LikedPlaylistSerializer,
    RulesSerializer,
    PlayConfigurationSerializer,
)
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.pagination import PageNumberPagination
from django.db.models import Sum, Count, F, IntegerField, Value, Prefetch, DecimalField
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.conf import settings
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import uuid
import os
import mimetypes
import random
import time
from mutagen.mp3 import MP3
from mutagen.wave import WAVE
from django.utils import timezone
from datetime import timedelta
from django.db.models import Q, Count, Avg, F
from django.shortcuts import get_object_or_404


class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        serializer = RegisterSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save()
            return Response(UserSerializer(user).data, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CustomTokenObtainPairView(TokenObtainPairView):
    permission_classes = [AllowAny]
    serializer_class = CustomTokenObtainPairSerializer


class CustomTokenRefreshView(TokenRefreshView):
    # uses SimpleJWT's TokenRefreshView; with ROTATE_REFRESH_TOKENS=True it will return a new refresh token too
    permission_classes = [AllowAny]


class UserProfileView(APIView):
    """Retrieve and Update User Profile"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = UserSerializer(request.user)
        return Response(serializer.data)

    def patch(self, request):
        serializer = UserSerializer(request.user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class NotificationSettingUpdateView(APIView):
    """Update User Notification Settings"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        setting, created = NotificationSetting.objects.get_or_create(user=request.user)
        serializer = NotificationSettingSerializer(setting)
        return Response(serializer.data)

    def put(self, request):
        setting, created = NotificationSetting.objects.get_or_create(user=request.user)
        serializer = NotificationSettingSerializer(setting, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request):
        setting, created = NotificationSetting.objects.get_or_create(user=request.user)
        serializer = NotificationSettingSerializer(setting, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class StreamQualityUpdateView(APIView):
    """Update User Stream Quality Settings"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            "stream_quality": request.user.stream_quality,
            "plan": request.user.plan
        })

    def put(self, request):
        quality = request.data.get('stream_quality')
        if quality not in ['medium', 'high']:
            return Response({"detail": "Invalid quality choice."}, status=status.HTTP_400_BAD_REQUEST)
        
        if quality == 'high' and request.user.plan != 'premium':
            return Response({"detail": "High quality streaming is only available for premium users."}, status=status.HTTP_403_FORBIDDEN)
        
        request.user.stream_quality = quality
        request.user.save(update_fields=['stream_quality'])
        return Response({"stream_quality": request.user.stream_quality})

    def patch(self, request):
        return self.put(request)


class UserFollowView(APIView):
    """Follow or Unfollow a User or Artist"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = FollowRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        user_id = serializer.validated_data.get('user_id')
        artist_id = serializer.validated_data.get('artist_id')
        
        follower = request.user
        # If the user has an artist profile, we could potentially follow as an artist.
        # For now, we follow as the User account as per "users only can post to it".
        # But we'll check if they want to follow as artist if we add that later.
        
        if user_id:
            target = get_object_or_404(User, id=user_id)
            if target == follower:
                return Response({'error': 'You cannot follow yourself.'}, status=status.HTTP_400_BAD_REQUEST)
            
            follow_qs = Follow.objects.filter(follower_user=follower, followed_user=target)
            if follow_qs.exists():
                follow_qs.delete()
                return Response({'status': 'ok', 'message': 'unfollowed'}, status=status.HTTP_200_OK)
            else:
                Follow.objects.create(follower_user=follower, followed_user=target)
                return Response({'status': 'ok', 'message': 'followed'}, status=status.HTTP_200_OK)
        
        if artist_id:
            target = get_object_or_404(Artist, id=artist_id)
            follow_qs = Follow.objects.filter(follower_user=follower, followed_artist=target)
            if follow_qs.exists():
                follow_qs.delete()
                return Response({'status': 'ok', 'message': 'unfollowed'}, status=status.HTTP_200_OK)
            else:
                Follow.objects.create(follower_user=follower, followed_artist=target)
                return Response({'status': 'ok', 'message': 'followed'}, status=status.HTTP_200_OK)


class LikedSongsView(APIView):
    """List of songs liked by the user, paginated and sorted by date"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        qs = SongLike.objects.filter(user=user).order_by('-created_at')
        
        paginator = PageNumberPagination()
        paginator.page_size = 10
        result_page = paginator.paginate_queryset(qs, request)
        serializer = LikedSongSerializer(result_page, many=True, context={'request': request})
        
        return paginator.get_paginated_response(serializer.data)


class LikedAlbumsView(APIView):
    """List of albums liked by the user, paginated and sorted by date"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        qs = AlbumLike.objects.filter(user=user).order_by('-created_at')
        
        paginator = PageNumberPagination()
        paginator.page_size = 10
        result_page = paginator.paginate_queryset(qs, request)
        serializer = LikedAlbumSerializer(result_page, many=True, context={'request': request})
        
        return paginator.get_paginated_response(serializer.data)


class LikedPlaylistsView(APIView):
    """List of playlists liked by the user, paginated and sorted by date"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        qs = PlaylistLike.objects.filter(user=user).order_by('-created_at')
        
        paginator = PageNumberPagination()
        paginator.page_size = 10
        result_page = paginator.paginate_queryset(qs, request)
        serializer = LikedPlaylistSerializer(result_page, many=True, context={'request': request})
        
        return paginator.get_paginated_response(serializer.data)


class MyArtistsView(APIView):
    """List of artists followed by the user, paginated and sorted by date"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        # Filter follows where this user is the follower and the target is an artist
        qs = Follow.objects.filter(follower_user=user, followed_artist__isnull=False).order_by('-created_at')
        
        paginator = PageNumberPagination()
        paginator.page_size = 10
        result_page = paginator.paginate_queryset(qs, request)
        
        # We want to return the artist data, but we have Follow objects.
        # We can use a SerializerMethodField or just map them.
        results = []
        for follow in result_page:
            artist_data = ArtistSerializer(follow.followed_artist, context={'request': request}).data
            artist_data['followed_at'] = follow.created_at
            results.append(artist_data)
            
        return paginator.get_paginated_response(results)


class MyLibraryView(APIView):
    """
    User's library history.
    Supports 'mix' mode (all types) or specific 'type' param.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        content_type = request.query_params.get('type')
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))
        offset = (page - 1) * page_size

        qs = UserHistory.objects.filter(user=request.user).order_by('-updated_at')

        if content_type:
            if content_type not in [UserHistory.TYPE_SONG, UserHistory.TYPE_ALBUM, UserHistory.TYPE_PLAYLIST, UserHistory.TYPE_ARTIST]:
                return Response({"detail": "Invalid type."}, status=status.HTTP_400_BAD_REQUEST)
            qs = qs.filter(content_type=content_type)

        total = qs.count()
        items = qs[offset:offset + page_size]
        
        results = []
        for entry in items:
            data = {
                'id': entry.id,
                'type': entry.content_type,
                'viewed_at': entry.updated_at,
            }
            
            if entry.content_type == UserHistory.TYPE_SONG and entry.song:
                data['item'] = SongStreamSerializer(entry.song, context={'request': request}).data
            elif entry.content_type == UserHistory.TYPE_ALBUM and entry.album:
                data['item'] = AlbumSerializer(entry.album, context={'request': request}).data
            elif entry.content_type == UserHistory.TYPE_PLAYLIST and entry.playlist:
                data['item'] = PlaylistSerializer(entry.playlist, context={'request': request}).data
            elif entry.content_type == UserHistory.TYPE_ARTIST and entry.artist:
                data['item'] = ArtistSerializer(entry.artist, context={'request': request}).data
            else:
                continue
                
            results.append(data)

        return Response({
            'items': results,
            'total': total,
            'page': page,
            'has_next': total > offset + page_size
        })


class R2UploadView(APIView):
    """Upload a file to an S3-compatible R2 bucket and return a CDN URL."""
    permission_classes = [AllowAny]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, *args, **kwargs):
        serializer = UploadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        f = serializer.validated_data['file']
        folder = serializer.validated_data.get('folder', '').strip().strip('/')
        custom_filename = serializer.validated_data.get('filename')
        
        # get original filename and extension from uploaded file
        original_filename = getattr(f, 'name', None) or 'upload'
        
        if custom_filename:
            # if user provided custom filename, preserve extension from original file
            import os
            _, original_ext = os.path.splitext(original_filename)
            # check if custom filename already has an extension
            _, custom_ext = os.path.splitext(custom_filename)
            if custom_ext:
                # use custom filename as-is (user provided extension)
                filename = custom_filename
            else:
                # append original extension to custom filename
                filename = f"{custom_filename}{original_ext}"
        else:
            # no custom filename, use original
            filename = original_filename

        # build key: folder/filename (no unique prefix, use exact filename)
        key = f"{folder + '/' if folder else ''}{filename}"

        # Build boto3 client kwargs and avoid sending an empty session token
        client_kwargs = {
            'service_name': 's3',
            'endpoint_url': getattr(settings, 'R2_ENDPOINT_URL', None),
            'aws_access_key_id': getattr(settings, 'R2_ACCESS_KEY_ID', None),
            'aws_secret_access_key': getattr(settings, 'R2_SECRET_ACCESS_KEY', None),
            # Cloudflare R2 requires signature v4
            'config': Config(signature_version='s3v4'),
        }
        session_token = getattr(settings, 'R2_SESSION_TOKEN', None)
        if session_token:
            client_kwargs['aws_session_token'] = session_token

        # remove None values to avoid boto3 sending invalid headers
        client_kwargs = {k: v for k, v in client_kwargs.items() if v is not None}

        s3 = boto3.client(**client_kwargs)

        # Detect content type from file extension to preserve format
        import mimetypes
        content_type, _ = mimetypes.guess_type(filename)
        if not content_type:
            content_type = 'application/octet-stream'

        try:
            # upload_fileobj streams the file directly with content type
            s3.upload_fileobj(
                f, 
                getattr(settings, 'R2_BUCKET_NAME'), 
                key,
                ExtraArgs={'ContentType': content_type}
            )
        except ClientError as e:
            # Return a clearer error and include AWS error code/message
            err = e.response.get('Error', {})
            code = err.get('Code')
            msg = err.get('Message') or str(e)
            detail = f"{code}: {msg}" if code else str(e)
            # common cause: invalid/extra session token (X-Amz-Security-Token)
            if 'Security-Token' in detail or 'X-Amz-Security-Token' in detail:
                detail += ' — check R2_SESSION_TOKEN: remove it unless you are using temporary credentials.'
            return Response({'detail': detail}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        cdn_base = getattr(settings, 'R2_CDN_BASE', 'https://cdn.sedabox.com').rstrip('/')
        url = f"{cdn_base}/{key}"
        return Response({'key': key, 'url': url}, status=status.HTTP_201_CREATED)


def upload_file_to_r2(file_obj, folder='', custom_filename=None):
    """
    Helper function to upload a file to R2 and return the CDN URL.
    
    Args:
        file_obj: Django UploadedFile object
        folder: Optional folder path in bucket
        custom_filename: Optional custom filename (will preserve extension)
    
    Returns:
        tuple: (cdn_url, original_format) or raises Exception
    """
    original_filename = getattr(file_obj, 'name', None) or 'upload'
    
    if custom_filename:
        _, original_ext = os.path.splitext(original_filename)
        _, custom_ext = os.path.splitext(custom_filename)
        if custom_ext:
            filename = custom_filename
        else:
            filename = f"{custom_filename}{original_ext}"
    else:
        filename = original_filename
    
    # Get original format
    _, ext = os.path.splitext(original_filename)
    original_format = ext.lstrip('.').lower()
    
    # Build key
    key = f"{folder + '/' if folder else ''}{filename}"
    
    # Build boto3 client
    client_kwargs = {
        'service_name': 's3',
        'endpoint_url': getattr(settings, 'R2_ENDPOINT_URL', None),
        'aws_access_key_id': getattr(settings, 'R2_ACCESS_KEY_ID', None),
        'aws_secret_access_key': getattr(settings, 'R2_SECRET_ACCESS_KEY', None),
        'config': Config(signature_version='s3v4'),
    }
    session_token = getattr(settings, 'R2_SESSION_TOKEN', None)
    if session_token:
        client_kwargs['aws_session_token'] = session_token
    
    client_kwargs = {k: v for k, v in client_kwargs.items() if v is not None}
    s3 = boto3.client(**client_kwargs)
    
    # Detect content type
    content_type, _ = mimetypes.guess_type(filename)
    if not content_type:
        content_type = 'application/octet-stream'
    
    # Upload
    s3.upload_fileobj(
        file_obj,
        getattr(settings, 'R2_BUCKET_NAME'),
        key,
        ExtraArgs={'ContentType': content_type}
    )
    
    # Build CDN URL
    cdn_base = getattr(settings, 'R2_CDN_BASE', 'https://cdn.sedabox.com').rstrip('/')
    url = f"{cdn_base}/{key}"
    
    return url, original_format


def get_audio_duration(file_obj, format_ext):
    """
    Extract duration from audio file.
    
    Args:
        file_obj: Django UploadedFile object
        format_ext: File extension (mp3, wav, etc.)
    
    Returns:
        int: Duration in seconds or None
    """
    try:
        # Save to temporary file to read metadata
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{format_ext}') as tmp_file:
            for chunk in file_obj.chunks():
                tmp_file.write(chunk)
            tmp_path = tmp_file.name
        
        # Reset file pointer
        file_obj.seek(0)
        
        # Read duration based on format
        duration = None
        if format_ext == 'mp3':
            audio = MP3(tmp_path)
            duration = int(audio.info.length)
        elif format_ext == 'wav':
            audio = WAVE(tmp_path)
            duration = int(audio.info.length)
        
        # Clean up temp file
        os.unlink(tmp_path)
        
        return duration
    except Exception as e:
        print(f"Error extracting audio duration: {e}")
        return None


class SongUploadView(APIView):
    """
    Upload song with audio file and metadata.
    Accepts mp3 and wav files, uploads to R2, and creates Song record.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    
    def post(self, request, *args, **kwargs):
        serializer = SongUploadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        data = serializer.validated_data
        
        try:
            # Get artist
            artist = Artist.objects.get(id=data['artist_id'])
            
            # Build filename: "Artist - Title (feat. X)" or "Artist - Title"
            title = data['title']
            featured = data.get('featured_artists', [])
            if featured:
                filename_base = f"{artist.name} - {title} (feat. {', '.join(featured)})"
            else:
                filename_base = f"{artist.name} - {title}"
            
            # Upload audio file
            audio_file = data['audio_file']
            audio_filename = f"{filename_base}.{audio_file.name.split('.')[-1]}"
            audio_url, original_format = upload_file_to_r2(
                audio_file,
                folder='songs',
                custom_filename=audio_filename
            )
            
            # Get audio duration
            duration = get_audio_duration(audio_file, original_format)
            
            # Upload cover image if provided
            cover_url = ""
            if data.get('cover_image'):
                cover_file = data['cover_image']
                cover_filename = f"{filename_base}_cover.{cover_file.name.split('.')[-1]}"
                cover_url, _ = upload_file_to_r2(
                    cover_file,
                    folder='covers',
                    custom_filename=cover_filename
                )
            
            # Create song record
            song_data = {
                'title': title,
                'artist': artist,
                'featured_artists': featured,
                'audio_file': audio_url,
                'cover_image': cover_url,
                'original_format': original_format,
                'duration_seconds': duration,
                'uploader': request.user,
                'is_single': data.get('is_single', False),
                'release_date': data.get('release_date'),
                'language': data.get('language', 'fa'),
                'description': data.get('description', ''),
                'lyrics': data.get('lyrics', ''),
                'tempo': data.get('tempo'),
                'energy': data.get('energy'),
                'danceability': data.get('danceability'),
                'valence': data.get('valence'),
                'acousticness': data.get('acousticness'),
                'instrumentalness': data.get('instrumentalness'),
                'speechiness': data.get('speechiness'),
                'live_performed': data.get('live_performed', False),
                'label': data.get('label', ''),
                'producers': data.get('producers', []),
                'composers': data.get('composers', []),
                'lyricists': data.get('lyricists', []),
                'credits': data.get('credits', ''),
            }
            
            # Add album if provided
            if data.get('album_id'):
                song_data['album'] = Album.objects.get(id=data['album_id'])
            
            song = Song.objects.create(**song_data)
            
            # Add many-to-many relationships
            if data.get('genre_ids'):
                song.genres.set(Genre.objects.filter(id__in=data['genre_ids']))
            if data.get('sub_genre_ids'):
                song.sub_genres.set(SubGenre.objects.filter(id__in=data['sub_genre_ids']))
            if data.get('mood_ids'):
                song.moods.set(Mood.objects.filter(id__in=data['mood_ids']))
            if data.get('tag_ids'):
                song.tags.set(Tag.objects.filter(id__in=data['tag_ids']))
            
            return Response(
                SongSerializer(song).data,
                status=status.HTTP_201_CREATED
            )
            
        except Artist.DoesNotExist:
            return Response(
                {'error': 'Artist not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Album.DoesNotExist:
            return Response(
                {'error': 'Album not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ArtistListView(APIView):
    """List and Create Artists"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get(self, request):
        artists = Artist.objects.all()
        serializer = ArtistSerializer(artists, many=True, context={'request': request})
        return Response(serializer.data)

    def post(self, request):
        serializer = ArtistSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class PlaylistDetailView(APIView):
    """Retrieve, Update, and Delete Playlist (Admin/System/Audience)"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_object(self, pk):
        try:
            return Playlist.objects.get(pk=pk)
        except Playlist.DoesNotExist:
            return None

    def get(self, request, pk):
        playlist = self.get_object(pk)
        if not playlist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        
        # Record history
        if request.user.is_authenticated:
            UserHistory.objects.update_or_create(
                user=request.user,
                content_type=UserHistory.TYPE_PLAYLIST,
                playlist=playlist,
                defaults={'updated_at': timezone.now()}
            )
            
        serializer = PlaylistSerializer(playlist, context={'request': request})
        return Response(serializer.data)

    def put(self, request, pk):
        playlist = self.get_object(pk)
        if not playlist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = PlaylistSerializer(playlist, data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request, pk):
        playlist = self.get_object(pk)
        if not playlist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = PlaylistSerializer(playlist, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        playlist = self.get_object(pk)
        if not playlist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        playlist.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PlaylistLikeView(APIView):
    """Like or unlike a playlist (Admin/System/Audience)"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            playlist = Playlist.objects.get(pk=pk)
        except Playlist.DoesNotExist:
            return Response({"detail": "Playlist not found."}, status=status.HTTP_404_NOT_FOUND)
        
        user = request.user
        like_qs = PlaylistLike.objects.filter(user=user, playlist=playlist)
        if like_qs.exists():
            like_qs.delete()
            liked = False
        else:
            PlaylistLike.objects.create(user=user, playlist=playlist)
            liked = True
            
        return Response({
            "liked": liked,
            "likes_count": playlist.liked_by.count()
        })


class ArtistDetailView(APIView):
    """Retrieve, Update, and Delete Artist"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_object(self, pk):
        try:
            return Artist.objects.get(pk=pk)
        except Artist.DoesNotExist:
            return None

    def get(self, request, pk):
        artist = self.get_object(pk)
        if not artist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        
        # Record history
        if request.user.is_authenticated:
            UserHistory.objects.update_or_create(
                user=request.user,
                content_type=UserHistory.TYPE_ARTIST,
                artist=artist,
                defaults={'updated_at': timezone.now()}
            )
        
        # Pagination params
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 10))
        offset = (page - 1) * page_size
        
        # Check if user wants a specific list (paginated)
        list_type = request.query_params.get('type')
        
        if list_type == 'top_songs':
            qs = Song.objects.filter(artist=artist, status=Song.STATUS_PUBLISHED).annotate(
                total_plays=Coalesce(F('plays'), 0) + Count('play_counts')
            ).order_by('-total_plays')
            items = qs[offset:offset + page_size]
            data = SongStreamSerializer(items, many=True, context={'request': request}).data
            return Response({
                'items': data,
                'total': qs.count(),
                'page': page,
                'has_next': qs.count() > offset + page_size
            })
            
        if list_type == 'albums':
            qs = Album.objects.filter(artist=artist).exclude(title__iexact='single').order_by('-release_date')
            items = qs[offset:offset + page_size]
            data = AlbumSerializer(items, many=True, context={'request': request}).data
            return Response({
                'items': data,
                'total': qs.count(),
                'page': page,
                'has_next': qs.count() > offset + page_size
            })
            
        if list_type == 'latest_songs':
            qs = Song.objects.filter(artist=artist, status=Song.STATUS_PUBLISHED).order_by('-release_date', '-created_at')
            items = qs[offset:offset + page_size]
            data = SongStreamSerializer(items, many=True, context={'request': request}).data
            return Response({
                'items': data,
                'total': qs.count(),
                'page': page,
                'has_next': qs.count() > offset + page_size
            })

        # Default: Return full detail view
        # Basic artist data
        artist_data = ArtistSerializer(artist, context={'request': request}).data
        
        # 1. Top Songs (preview)
        top_songs_qs = Song.objects.filter(artist=artist, status=Song.STATUS_PUBLISHED).annotate(
            total_plays=Coalesce(F('plays'), 0) + Count('play_counts')
        ).order_by('-total_plays')
        top_songs_data = SongStreamSerializer(top_songs_qs[:5], many=True, context={'request': request}).data
        
        # 2. Albums (preview)
        albums_qs = Album.objects.filter(artist=artist).exclude(title__iexact='single').order_by('-release_date')
        albums_data = AlbumSerializer(albums_qs[:5], many=True, context={'request': request}).data
        
        # 3. Latest Songs (preview)
        latest_songs_qs = Song.objects.filter(artist=artist, status=Song.STATUS_PUBLISHED).order_by('-release_date', '-created_at')
        latest_songs_data = SongStreamSerializer(latest_songs_qs[:5], many=True, context={'request': request}).data
        
        # 4. Discovered On
        discovered_on = []
        
        # Admin playlists
        admin_playlists = Playlist.objects.filter(songs__artist=artist, created_by=Playlist.CREATED_BY_ADMIN).distinct()
        # System/Audience playlists with likes
        other_playlists = Playlist.objects.filter(
            songs__artist=artist,
            created_by__in=[Playlist.CREATED_BY_SYSTEM, Playlist.CREATED_BY_AUDIENCE]
        ).annotate(likes_count=Count('liked_by')).filter(likes_count__gt=0).distinct()
        # Public UserPlaylists with likes
        user_playlists = UserPlaylist.objects.filter(songs__artist=artist, public=True).annotate(likes_count=Count('liked_by')).filter(likes_count__gt=0).distinct()
        
        # Credited songs
        credited_songs = Song.objects.filter(
            Q(featured_artists__icontains=artist.name) |
            Q(producers__icontains=artist.name) |
            Q(composers__icontains=artist.name) |
            Q(lyricists__icontains=artist.name)
        ).exclude(artist=artist).filter(status=Song.STATUS_PUBLISHED).distinct()
        
        for p in admin_playlists:
            discovered_on.append({'type': 'playlist', 'id': p.id, 'title': p.title, 'image': p.cover_image, 'source': 'admin'})
        for p in other_playlists:
            discovered_on.append({'type': 'playlist', 'id': p.id, 'title': p.title, 'image': p.cover_image, 'source': p.created_by})
        for p in user_playlists:
            discovered_on.append({'type': 'user_playlist', 'id': p.id, 'title': p.title, 'image': None, 'source': 'user'})
        for s in credited_songs:
            discovered_on.append({'type': 'song', 'id': s.id, 'title': s.title, 'image': s.cover_image, 'artist': s.artist.name if s.artist else None})

        base_url = request.build_absolute_uri(request.path)
        
        return Response({
            'artist': artist_data,
            'top_songs': {
                'items': top_songs_data,
                'total': top_songs_qs.count(),
                'next_page_link': f"{base_url}?type=top_songs&page=2" if top_songs_qs.count() > 5 else None
            },
            'albums': {
                'items': albums_data,
                'total': albums_qs.count(),
                'next_page_link': f"{base_url}?type=albums&page=2" if albums_qs.count() > 5 else None
            },
            'latest_songs': {
                'items': latest_songs_data,
                'total': latest_songs_qs.count(),
                'next_page_link': f"{base_url}?type=latest_songs&page=2" if latest_songs_qs.count() > 5 else None
            },
            'discovered_on': discovered_on[:10]
        })

    def put(self, request, pk):
        artist = self.get_object(pk)
        if not artist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = ArtistSerializer(artist, data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request, pk):
        artist = self.get_object(pk)
        if not artist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = ArtistSerializer(artist, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        artist = self.get_object(pk)
        if not artist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        artist.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AlbumListView(APIView):
    """List and Create Albums"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get(self, request):
        albums = Album.objects.all()
        serializer = AlbumSerializer(albums, many=True, context={'request': request})
        return Response(serializer.data)

    def post(self, request):
        serializer = AlbumSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class AlbumDetailView(APIView):
    """Retrieve, Update, and Delete Album"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_object(self, pk):
        try:
            return Album.objects.get(pk=pk)
        except Album.DoesNotExist:
            return None

    def get(self, request, pk):
        album = self.get_object(pk)
        if not album:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        
        # Record history
        if request.user.is_authenticated:
            UserHistory.objects.update_or_create(
                user=request.user,
                content_type=UserHistory.TYPE_ALBUM,
                album=album,
                defaults={'updated_at': timezone.now()}
            )
            
        serializer = AlbumSerializer(album, context={'request': request})
        return Response(serializer.data)

    def put(self, request, pk):
        album = self.get_object(pk)
        if not album:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = AlbumSerializer(album, data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request, pk):
        album = self.get_object(pk)
        if not album:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = AlbumSerializer(album, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        album = self.get_object(pk)
        if not album:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        album.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class GenreListView(APIView):
    """List and Create Genres"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get(self, request):
        genres = Genre.objects.all()
        serializer = GenreSerializer(genres, many=True, context={'request': request})
        return Response(serializer.data)

    def post(self, request):
        serializer = GenreSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class GenreDetailView(APIView):
    """Retrieve, Update, and Delete Genre"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_object(self, pk):
        try:
            return Genre.objects.get(pk=pk)
        except Genre.DoesNotExist:
            return None

    def get(self, request, pk):
        genre = self.get_object(pk)
        if not genre:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = GenreSerializer(genre, context={'request': request})
        return Response(serializer.data)

    def put(self, request, pk):
        genre = self.get_object(pk)
        if not genre:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = GenreSerializer(genre, data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request, pk):
        genre = self.get_object(pk)
        if not genre:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = GenreSerializer(genre, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        genre = self.get_object(pk)
        if not genre:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        genre.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class MoodListView(APIView):
    """List and Create Moods"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get(self, request):
        moods = Mood.objects.all()
        serializer = MoodSerializer(moods, many=True, context={'request': request})
        return Response(serializer.data)

    def post(self, request):
        serializer = MoodSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class MoodDetailView(APIView):
    """Retrieve, Update, and Delete Mood"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_object(self, pk):
        try:
            return Mood.objects.get(pk=pk)
        except Mood.DoesNotExist:
            return None

    def get(self, request, pk):
        mood = self.get_object(pk)
        if not mood:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = MoodSerializer(mood, context={'request': request})
        return Response(serializer.data)

    def put(self, request, pk):
        mood = self.get_object(pk)
        if not mood:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = MoodSerializer(mood, data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request, pk):
        mood = self.get_object(pk)
        if not mood:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = MoodSerializer(mood, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        mood = self.get_object(pk)
        if not mood:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        mood.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TagListView(APIView):
    """List and Create Tags"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get(self, request):
        tags = Tag.objects.all()
        serializer = TagSerializer(tags, many=True, context={'request': request})
        return Response(serializer.data)

    def post(self, request):
        serializer = TagSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class TagDetailView(APIView):
    """Retrieve, Update, and Delete Tag"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_object(self, pk):
        try:
            return Tag.objects.get(pk=pk)
        except Tag.DoesNotExist:
            return None

    def get(self, request, pk):
        tag = self.get_object(pk)
        if not tag:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = TagSerializer(tag, context={'request': request})
        return Response(serializer.data)

    def put(self, request, pk):
        tag = self.get_object(pk)
        if not tag:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = TagSerializer(tag, data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request, pk):
        tag = self.get_object(pk)
        if not tag:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = TagSerializer(tag, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        tag = self.get_object(pk)
        if not tag:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        tag.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SubGenreListView(APIView):
    """List and Create SubGenres"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get(self, request):
        subgenres = SubGenre.objects.all()
        serializer = SubGenreSerializer(subgenres, many=True, context={'request': request})
        return Response(serializer.data)

    def post(self, request):
        serializer = SubGenreSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SubGenreDetailView(APIView):
    """Retrieve, Update, and Delete SubGenre"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_object(self, pk):
        try:
            return SubGenre.objects.get(pk=pk)
        except SubGenre.DoesNotExist:
            return None

    def get(self, request, pk):
        subgenre = self.get_object(pk)
        if not subgenre:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = SubGenreSerializer(subgenre, context={'request': request})
        return Response(serializer.data)

    def put(self, request, pk):
        subgenre = self.get_object(pk)
        if not subgenre:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = SubGenreSerializer(subgenre, data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request, pk):
        subgenre = self.get_object(pk)
        if not subgenre:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = SubGenreSerializer(subgenre, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        subgenre = self.get_object(pk)
        if not subgenre:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        subgenre.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SongListView(generics.ListCreateAPIView):
    """View for listing and creating songs"""
    serializer_class = SongSerializer
    permission_classes = [IsAuthenticated]
    
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return super().get_permissions()
    
    def get_queryset(self):
        """Filter songs by status for non-staff users"""
        queryset = Song.objects.all()
        
        # Non-authenticated or non-staff users only see published songs
        if not self.request.user.is_authenticated or not self.request.user.is_staff:
            queryset = queryset.filter(status=Song.STATUS_PUBLISHED)
        
        return queryset


class SongDetailView(APIView):
    """View for retrieving, updating and deleting a song"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_object(self, pk):
        try:
            song = Song.objects.get(pk=pk)
            # Non-authenticated or non-staff users only see published songs
            if not self.request.user.is_authenticated or not self.request.user.is_staff:
                if song.status != Song.STATUS_PUBLISHED:
                    return None
            return song
        except Song.DoesNotExist:
            return None

    def get(self, request, pk):
        song = self.get_object(pk)
        if not song:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        
        # Record history
        if request.user.is_authenticated:
            UserHistory.objects.update_or_create(
                user=request.user,
                content_type=UserHistory.TYPE_SONG,
                song=song,
                defaults={'updated_at': timezone.now()}
            )
            
        serializer = SongSerializer(song, context={'request': request})
        return Response(serializer.data)

    def put(self, request, pk):
        song = self.get_object(pk)
        if not song:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = SongSerializer(song, data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request, pk):
        song = self.get_object(pk)
        if not song:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = SongSerializer(song, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        song = self.get_object(pk)
        if not song:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        song.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SongLikeView(APIView):
    """Toggle like status for a song"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk=None):
        try:
            song = Song.objects.get(pk=pk)
        except Song.DoesNotExist:
            return Response({'error': 'Song not found'}, status=status.HTTP_404_NOT_FOUND)
            
        user = request.user
        like_qs = SongLike.objects.filter(user=user, song=song)
        if like_qs.exists():
            like_qs.delete()
            liked = False
        else:
            SongLike.objects.create(user=user, song=song)
            liked = True
            
        return Response({
            'liked': liked,
            'likes_count': SongLike.objects.filter(song=song).count()
        })


class AlbumLikeView(APIView):
    """Toggle like status for an album"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk=None):
        try:
            album = Album.objects.get(pk=pk)
        except Album.DoesNotExist:
            return Response({'error': 'Album not found'}, status=status.HTTP_404_NOT_FOUND)
            
        user = request.user
        like_qs = AlbumLike.objects.filter(user=user, album=album)
        if like_qs.exists():
            like_qs.delete()
            liked = False
        else:
            AlbumLike.objects.create(user=user, album=album)
            liked = True
            
        return Response({
            'liked': liked,
            'likes_count': AlbumLike.objects.filter(album=album).count()
        })


class SongIncrementPlaysView(APIView):
    """Increment play count for a song"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk=None):
        try:
            song = Song.objects.get(pk=pk)
        except Song.DoesNotExist:
            return Response({'error': 'Song not found'}, status=status.HTTP_404_NOT_FOUND)
            
        song.plays += 1
        song.save(update_fields=['plays'])
        return Response({'plays': song.plays})


class SongStreamListView(generics.ListAPIView):
    """
    List songs with wrapper stream URLs that require unwrapping.
    Returns songs with stream_url field that points to unwrap endpoint.
    """
    serializer_class = SongStreamSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        """Filter songs by status for non-staff users"""
        queryset = Song.objects.all()
        
        # Non-staff users only see published songs
        if not self.request.user.is_staff:
            queryset = queryset.filter(status=Song.STATUS_PUBLISHED)
        
        # Filter by artist
        artist_id = self.request.query_params.get('artist')
        if artist_id:
            queryset = queryset.filter(artist_id=artist_id)
        
        # Filter by album
        album_id = self.request.query_params.get('album')
        if album_id:
            queryset = queryset.filter(album_id=album_id)
        
        # Filter by genre
        genre_id = self.request.query_params.get('genre')
        if genre_id:
            queryset = queryset.filter(genres__id=genre_id)
        
        # Filter by mood
        mood_id = self.request.query_params.get('mood')
        if mood_id:
            queryset = queryset.filter(moods__id=mood_id)
        
        return queryset.distinct()


def generate_signed_r2_url(object_key, expiration=3600):
    """
    Generate a short-lived signed URL for R2 object.
    
    Args:
        object_key: The key of the object in R2 bucket (e.g., 'songs/artist-title.mp3')
        expiration: URL expiration time in seconds (default 1 hour)
    
    Returns:
        str: Signed URL
    """
    client_kwargs = {
        'service_name': 's3',
        'endpoint_url': getattr(settings, 'R2_ENDPOINT_URL', None),
        'aws_access_key_id': getattr(settings, 'R2_ACCESS_KEY_ID', None),
        'aws_secret_access_key': getattr(settings, 'R2_SECRET_ACCESS_KEY', None),
        'config': Config(signature_version='s3v4'),
    }
    session_token = getattr(settings, 'R2_SESSION_TOKEN', None)
    if session_token:
        client_kwargs['aws_session_token'] = session_token
    
    client_kwargs = {k: v for k, v in client_kwargs.items() if v is not None}
    s3 = boto3.client(**client_kwargs)
    
    bucket_name = getattr(settings, 'R2_BUCKET_NAME')
    
    # Generate presigned URL
    signed_url = s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket_name, 'Key': object_key},
        ExpiresIn=expiration
    )
    
    return signed_url


class UnwrapStreamView(APIView):
    """
    Unwrap a stream URL token to get the actual signed URL.
    Tracks unwraps and injects ad URLs every 15 unwraps.
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request, token):
        try:
            # Get the stream access record
            stream_access = StreamAccess.objects.select_related('song', 'user').get(
                unwrap_token=token,
                user=request.user
            )
            
            # Check if already unwrapped
            if stream_access.unwrapped:
                return Response(
                    {'error': 'This stream token has already been used'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Mark as unwrapped
            stream_access.unwrapped = True
            stream_access.unwrapped_at = timezone.now()
            stream_access.save(update_fields=['unwrapped', 'unwrapped_at'])
            
            # Record active playback for live listener count
            from .models import ActivePlayback
            # Delete previous records for this user (only one active song at a time)
            ActivePlayback.objects.filter(user=request.user).delete()
            
            # Calculate expiration time based on song duration
            duration = stream_access.song.duration_seconds or 0
            expiration_time = timezone.now() + timedelta(seconds=duration)
            
            ActivePlayback.objects.create(
                user=request.user,
                song=stream_access.song,
                expiration_time=expiration_time
            )

            # Count unwrapped streams for this user (last 24 hours for fairness)
            cutoff_time = timezone.now() - timedelta(hours=24)
            unwrapped_count = StreamAccess.objects.filter(
                user=request.user,
                unwrapped=True,
                unwrapped_at__gte=cutoff_time
            ).count()
            
            # Every 15th unwrap gets an ad
            if unwrapped_count % 15 == 0:
                ad_url = getattr(settings, 'AD_URL', 'https://cdn.sedabox.com/ads/default-ad.mp3')
                return Response({
                    'type': 'ad',
                    'url': ad_url,
                    'message': 'Please listen to this brief advertisement',
                    'duration': 30,  # ad duration in seconds
                    'unwrap_count': unwrapped_count
                })
            
            # Extract object key from audio_file URL
            audio_url = stream_access.song.audio_file
            cdn_base = getattr(settings, 'R2_CDN_BASE', 'https://cdn.sedabox.com').rstrip('/')
            
            # Extract key from CDN URL and decode it properly
            if audio_url.startswith(cdn_base):
                object_key = audio_url.replace(cdn_base + '/', '')
                # URL decode the key to handle encoded characters
                from urllib.parse import unquote
                object_key = unquote(object_key)
            else:
                # Fallback: try to extract path and decode
                from urllib.parse import urlparse, unquote
                parsed = urlparse(audio_url)
                object_key = unquote(parsed.path.lstrip('/'))
            
            # If the stored audio URL points into our CDN (R2), generate a presigned R2 URL.
            # Otherwise return the original audio_url as-is (external/public URL).
            cdn_base = getattr(settings, 'R2_CDN_BASE', 'https://cdn.sedabox.com').rstrip('/')
            if audio_url and audio_url.startswith(cdn_base):
                signed_url = generate_signed_r2_url(object_key, expiration=3600)
            else:
                # return original URL (no signing) for external hosts
                signed_url = audio_url

            return Response({
                'type': 'stream',
                'url': signed_url,
                'song_id': stream_access.song.id,
                'song_title': stream_access.song.display_title,
                'expires_in': 3600 if signed_url and signed_url.startswith(cdn_base) else None,
                'unwrap_count': unwrapped_count
            })
            
        except StreamAccess.DoesNotExist:
            return Response(
                {'error': 'Invalid or unauthorized stream token'},
                status=status.HTTP_404_NOT_FOUND
            )


class StreamShortRedirectView(APIView):
    """
    Short URL redirect that generates signed URL on-the-fly.
    Much shorter URLs while maintaining security and ad injection.
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request, token):
        try:
            # Get the stream access record
            stream_access = StreamAccess.objects.select_related('song', 'user').get(
                short_token=token,
                user=request.user
            )
            
            # Check if already unwrapped
            if stream_access.unwrapped:
                return Response(
                    {'error': 'This stream URL has already been used'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Mark as unwrapped
            stream_access.unwrapped = True
            stream_access.unwrapped_at = timezone.now()
            stream_access.save(update_fields=['unwrapped', 'unwrapped_at'])
            
            # Count unwrapped streams for this user (last 24 hours for fairness)
            cutoff_time = timezone.now() - timedelta(hours=24)
            unwrapped_count = StreamAccess.objects.filter(
                user=request.user,
                unwrapped=True,
                unwrapped_at__gte=cutoff_time
            ).count()
            
            # Every 15th unwrap gets an ad
            if unwrapped_count % 15 == 0:
                ad_url = getattr(settings, 'AD_URL', 'https://cdn.sedabox.com/ads/default-ad.mp3')
                # Return JSON response for ad (can't redirect to ad)
                return Response({
                    'type': 'ad',
                    'url': ad_url,
                    'message': 'Please listen to this brief advertisement',
                    'duration': 30,
                    'unwrap_count': unwrapped_count
                })
            
            # Generate object key for R2
            audio_url = stream_access.song.audio_file
            cdn_base = getattr(settings, 'R2_CDN_BASE', 'https://cdn.sedabox.com').rstrip('/')
            
            # Extract key from CDN URL and decode it properly
            if audio_url.startswith(cdn_base):
                object_key = audio_url.replace(cdn_base + '/', '')
                # URL decode the key to handle encoded characters
                from urllib.parse import unquote
                object_key = unquote(object_key)
            else:
                # Fallback: try to extract path and decode
                from urllib.parse import urlparse, unquote
                parsed = urlparse(audio_url)
                object_key = unquote(parsed.path.lstrip('/'))
            
            # Generate signed URL and return it
            # Instead of returning the presigned R2 URL directly (which would be reusable),
            # create a one-time server-controlled access token and return a link to it.
            import secrets
            from django.urls import reverse
            # create unique one-time token (avoid collisions)
            for _ in range(5):
                one_time_token = secrets.token_urlsafe(32)
                if not StreamAccess.objects.filter(one_time_token=one_time_token).exists():
                    break
            else:
                # fallback to uuid
                one_time_token = uuid.uuid4().hex

            stream_access.one_time_token = one_time_token
            stream_access.one_time_used = False
            stream_access.one_time_expires_at = timezone.now() + timedelta(seconds=3600)
            stream_access.save(update_fields=['one_time_token', 'one_time_used', 'one_time_expires_at'])

            # Record active playback for live listener count
            from .models import ActivePlayback
            # Delete previous records for this user (only one active song at a time)
            ActivePlayback.objects.filter(user=request.user).delete()
            
            # Calculate expiration time based on song duration
            duration = stream_access.song.duration_seconds or 0
            expiration_time = timezone.now() + timedelta(seconds=duration)
            
            ActivePlayback.objects.create(
                user=request.user,
                song=stream_access.song,
                expiration_time=expiration_time
            )

            access_path = reverse('stream-access', kwargs={'token': one_time_token})
            access_url = request.build_absolute_uri(access_path)
            if access_url.startswith('http://'):
                access_url = access_url.replace('http://', 'https://', 1)

            # Also generate a signed URL for immediate use and return it (still store one-time token)
            cdn_base = getattr(settings, 'R2_CDN_BASE', 'https://cdn.sedabox.com').rstrip('/')
            if audio_url and audio_url.startswith(cdn_base):
                signed_url = generate_signed_r2_url(object_key, expiration=3600)
                expires = 3600
            else:
                signed_url = audio_url
                expires = None

            return Response({
                'type': 'stream',
                'url': signed_url,
                'song_id': stream_access.song.id,
                'song_title': stream_access.song.display_title,
                'expires_in': expires,
                'unwrap_count': unwrapped_count,
                'unique_otplay_id': stream_access.unique_otplay_id
            })
            
        except StreamAccess.DoesNotExist:
            return Response(
                {'error': 'Invalid or unauthorized stream URL'},
                status=status.HTTP_404_NOT_FOUND
            )


class StreamAccessView(APIView):
    """One-time access endpoint: redirects once to a presigned R2 URL and then becomes invalid."""
    permission_classes = [IsAuthenticated]

    def get(self, request, token):
        try:
            stream_access = StreamAccess.objects.select_related('song', 'user').get(
                one_time_token=token,
                user=request.user
            )

            # Check token expiry and usage
            if stream_access.one_time_used:
                return Response({'error': 'This one-time access URL has already been used'}, status=status.HTTP_400_BAD_REQUEST)

            if stream_access.one_time_expires_at and timezone.now() > stream_access.one_time_expires_at:
                return Response({'error': 'This one-time access URL has expired'}, status=status.HTTP_410_GONE)

            # Mark used before redirecting (best-effort; race-conditions remain small)
            stream_access.one_time_used = True
            stream_access.save(update_fields=['one_time_used'])

            # Build presigned R2 URL and redirect
            audio_url = stream_access.song.audio_file
            cdn_base = getattr(settings, 'R2_CDN_BASE', 'https://cdn.sedabox.com').rstrip('/')
            if audio_url.startswith(cdn_base):
                from urllib.parse import unquote
                object_key = unquote(audio_url.replace(cdn_base + '/', ''))
            else:
                from urllib.parse import urlparse, unquote
                parsed = urlparse(audio_url)
                object_key = unquote(parsed.path.lstrip('/'))

            signed_url = generate_signed_r2_url(object_key, expiration=3600)
            from django.http import HttpResponseRedirect
            return HttpResponseRedirect(signed_url)

        except StreamAccess.DoesNotExist:
            return Response({'error': 'Invalid or unauthorized one-time token'}, status=status.HTTP_404_NOT_FOUND)


def get_client_ip(request):
    """Get the client IP address from the request."""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip


class PlayCountView(APIView):
    """Endpoint to record play counts for songs."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({'error': 'Method GET not allowed. Please use POST.'}, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def post(self, request):
        unique_otplay_id = request.data.get('unique_otplay_id')
        city = request.data.get('city')
        country = request.data.get('country')

        if not all([unique_otplay_id, city, country]):
            return Response({'error': 'unique_otplay_id, city, and country are required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            stream_access = StreamAccess.objects.get(unique_otplay_id=unique_otplay_id, user=request.user)
            if stream_access.one_time_used:
                return Response({'error': 'This play ID has already been used'}, status=status.HTTP_400_BAD_REQUEST)

            song = stream_access.song
            ip = get_client_ip(request)

            # Get latest configuration
            config = PlayConfiguration.objects.last()
            pay_value = 0.000000
            if config:
                if request.user.plan == User.PLAN_PREMIUM:
                    pay_value = config.premium_play_worth
                else:
                    pay_value = config.free_play_worth

            play_count = PlayCount.objects.create(
                user=request.user,
                country=country,
                city=city,
                ip=ip,
                pay=pay_value
            )
            song.play_counts.add(play_count)

            # Mark as used
            stream_access.one_time_used = True
            stream_access.save(update_fields=['one_time_used'])

            # Update monthly listener record for the artist
            if song.artist:
                ArtistMonthlyListener.objects.update_or_create(
                    artist=song.artist,
                    user=request.user
                )

            return Response({'message': 'Play count recorded successfully'})

        except StreamAccess.DoesNotExist:
            return Response({'error': 'Invalid unique_otplay_id'}, status=status.HTTP_400_BAD_REQUEST)


class UserPlaylistListCreateView(APIView):
    """List all user playlists or create a new one"""
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """List user's playlists"""
        playlists = UserPlaylist.objects.filter(user=request.user)
        serializer = UserPlaylistSerializer(playlists, many=True, context={'request': request})
        return Response(serializer.data)
    
    def post(self, request):
        """Create a new playlist, optionally with first song"""
        serializer = UserPlaylistCreateSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            playlist = serializer.save()
            response_serializer = UserPlaylistSerializer(playlist, context={'request': request})
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class UserPlaylistDetailView(APIView):
    """Retrieve, update, or delete a specific user playlist"""
    permission_classes = [IsAuthenticated]
    
    def get_object(self, pk, user):
        try:
            return UserPlaylist.objects.get(pk=pk, user=user)
        except UserPlaylist.DoesNotExist:
            return None
    
    def get(self, request, pk):
        """Retrieve a playlist"""
        playlist = self.get_object(pk, request.user)
        if not playlist:
            return Response({'error': 'Playlist not found'}, status=status.HTTP_404_NOT_FOUND)
        serializer = UserPlaylistSerializer(playlist, context={'request': request})
        return Response(serializer.data)
    
    def put(self, request, pk):
        """Update a playlist"""
        playlist = self.get_object(pk, request.user)
        if not playlist:
            return Response({'error': 'Playlist not found'}, status=status.HTTP_404_NOT_FOUND)
        serializer = UserPlaylistSerializer(playlist, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    def delete(self, request, pk):
        """Delete a playlist"""
        playlist = self.get_object(pk, request.user)
        if not playlist:
            return Response({'error': 'Playlist not found'}, status=status.HTTP_404_NOT_FOUND)
        playlist.delete()
        return Response({'message': 'Playlist deleted successfully'}, status=status.HTTP_204_NO_CONTENT)


class UserPlaylistAddSongView(APIView):
    """Add a song to a user playlist"""
    permission_classes = [IsAuthenticated]
    
    def post(self, request, pk):
        """Add song to playlist"""
        try:
            playlist = UserPlaylist.objects.get(pk=pk, user=request.user)
        except UserPlaylist.DoesNotExist:
            return Response({'error': 'Playlist not found'}, status=status.HTTP_404_NOT_FOUND)
        
        song_id = request.data.get('song_id')
        if not song_id:
            return Response({'error': 'song_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            song = Song.objects.get(id=song_id)
            playlist.songs.add(song)
            serializer = UserPlaylistSerializer(playlist, context={'request': request})
            return Response(serializer.data)
        except Song.DoesNotExist:
            return Response({'error': 'Song not found'}, status=status.HTTP_404_NOT_FOUND)


class UserPlaylistRemoveSongView(APIView):
    """Remove a song from a user playlist"""
    permission_classes = [IsAuthenticated]
    
    def delete(self, request, pk, song_id):
        """Remove song from playlist"""
        try:
            playlist = UserPlaylist.objects.get(pk=pk, user=request.user)
        except UserPlaylist.DoesNotExist:
            return Response({'error': 'Playlist not found'}, status=status.HTTP_404_NOT_FOUND)
        
        try:
            song = Song.objects.get(id=song_id)
            playlist.songs.remove(song)
            serializer = UserPlaylistSerializer(playlist, context={'request': request})
            return Response(serializer.data)
        except Song.DoesNotExist:
            return Response({'error': 'Song not found'}, status=status.HTTP_404_NOT_FOUND)


class UserRecommendationView(APIView):
    """
    Spotify-level recommendation engine.
    Provides 10 songs based on user history (likes, plays, playlists)
    and metadata similarity.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        
        # 1. Get user's interaction history
        liked_song_ids = set(Song.objects.filter(liked_by=user).values_list('id', flat=True))
        played_song_ids = set(PlayCount.objects.filter(user=user).values_list('songs__id', flat=True))
        playlist_song_ids = set(UserPlaylist.objects.filter(user=user).values_list('songs__id', flat=True))
        
        all_interacted_ids = liked_song_ids | played_song_ids | playlist_song_ids
        
        # If no history, return trending songs
        if not all_interacted_ids:
            trending_songs = Song.objects.filter(status=Song.STATUS_PUBLISHED).order_by('-plays')[:10]
            serializer = SongSerializer(trending_songs, many=True, context={'request': request})
            return Response({
                'type': 'trending',
                'message': 'Start listening to get personalized recommendations!',
                'songs': serializer.data
            })

        # 2. Extract preferences
        interacted_songs = Song.objects.filter(id__in=all_interacted_ids)
        
        top_genres = interacted_songs.values('genres').annotate(count=Count('genres')).order_by('-count')[:3]
        top_moods = interacted_songs.values('moods').annotate(count=Count('moods')).order_by('-count')[:3]
        top_artists = interacted_songs.values('artist').annotate(count=Count('artist')).order_by('-count')[:3]
        top_languages = interacted_songs.values('language').annotate(count=Count('language')).order_by('-count')[:2]
        
        genre_ids = [g['genres'] for g in top_genres if g['genres']]
        mood_ids = [m['moods'] for m in top_moods if m['moods']]
        artist_ids = [a['artist'] for a in top_artists if a['artist']]
        preferred_languages = [l['language'] for l in top_languages if l['language']]
        
        # Average audio features
        avg_features = interacted_songs.aggregate(
            avg_energy=Avg('energy'),
            avg_dance=Avg('danceability'),
            avg_valence=Avg('valence'),
            avg_tempo=Avg('tempo')
        )

        # 3. Candidate Generation
        # Find songs that match top genres, moods, artists, or languages but haven't been interacted with
        candidates = Song.objects.filter(
            status=Song.STATUS_PUBLISHED
        ).exclude(
            id__in=all_interacted_ids
        ).filter(
            Q(genres__in=genre_ids) | 
            Q(moods__in=mood_ids) | 
            Q(artist__in=artist_ids) |
            Q(language__in=preferred_languages)
        ).distinct()

        # 4. Scoring & Ranking
        # We'll use a simple weighted scoring system in Python for better control
        scored_candidates = []
        
        for song in candidates[:100]: # Limit to 100 candidates for performance
            score = 0
            
            # Metadata matching
            song_genres = set(song.genres.values_list('id', flat=True))
            song_moods = set(song.moods.values_list('id', flat=True))
            
            score += len(song_genres.intersection(genre_ids)) * 3
            score += len(song_moods.intersection(mood_ids)) * 2
            if song.artist_id in artist_ids:
                score += 5
            if song.language in preferred_languages:
                score += 4  # Language match: 4 points (strong preference signal)
                
            # Audio feature similarity (inverse distance)
            if avg_features['avg_energy'] and song.energy:
                score += (100 - abs(song.energy - avg_features['avg_energy'])) / 10
            if avg_features['avg_dance'] and song.danceability:
                score += (100 - abs(song.danceability - avg_features['avg_dance'])) / 10
                
            scored_candidates.append((song, score))
            
        # Sort by score descending
        scored_candidates.sort(key=lambda x: x[1], reverse=True)
        
        # Take top 10
        recommended_songs = [item[0] for item in scored_candidates[:10]]
        
        # If we don't have enough recommendations, fill with trending
        if len(recommended_songs) < 10:
            needed = 10 - len(recommended_songs)
            trending = Song.objects.filter(status=Song.STATUS_PUBLISHED).exclude(
                id__in=all_interacted_ids
            ).exclude(
                id__in=[s.id for s in recommended_songs]
            ).order_by('-plays')[:needed]
            recommended_songs.extend(list(trending))

        serializer = SongSerializer(recommended_songs, many=True, context={'request': request})
        return Response({
            'type': 'personalized',
            'songs': serializer.data
        })


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class LatestReleasesView(generics.ListAPIView):
    """Return songs ordered by release date (newest first), paginated with next link."""
    serializer_class = SongSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardResultsSetPagination

    def get_permissions(self):
        # Allow unauthenticated GET access similar to other song endpoints
        if self.request.method == 'GET':
            return [permissions.AllowAny()]
        return super().get_permissions()

    def get_queryset(self):
        queryset = Song.objects.all()
        # Non-authenticated or non-staff users only see published songs
        if not self.request.user.is_authenticated or not self.request.user.is_staff:
            queryset = queryset.filter(status=Song.STATUS_PUBLISHED)

        # Order by release_date newest first, fall back to created_at for tie-breaker
        # Use NULLS LAST behavior where supported by the DB driver
        try:
            from django.db.models import F
            # annotate won't change ordering for nulls handling across DBs reliably,
            # so default to simple ordering which is acceptable in many setups
            queryset = queryset.order_by(F('release_date').desc(nulls_last=True), '-created_at')
        except Exception:
            queryset = queryset.order_by('-release_date', '-created_at')

        return queryset.distinct()


class PopularArtistsView(generics.ListAPIView):
    """Return artists ordered by a popularity score (plays + likes + playlist adds)."""
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardResultsSetPagination
    serializer_class = PopularArtistSerializer

    def get_permissions(self):
        # Allow public GET access similar to other endpoints
        if self.request.method == 'GET':
            return [permissions.AllowAny()]
        return super().get_permissions()

    def get_queryset(self):
        # Annotate artists with summed metrics across their songs
        queryset = Artist.objects.all()

        # total plays across songs
        queryset = queryset.annotate(
            total_plays=Coalesce(Sum('songs__plays'), 0)
        )

        # total likes across songs (may count each like instance)
        queryset = queryset.annotate(
            total_likes=Coalesce(Count('songs__liked_by'), 0)
        )

        # items added to playlists: count occurrences in both Playlist and UserPlaylist
        queryset = queryset.annotate(
            playlists_count=Coalesce(Count('songs__playlists'), 0),
            user_playlists_count=Coalesce(Count('songs__user_playlists'), 0),
        ).annotate(
            total_playlist_adds=F('playlists_count') + F('user_playlists_count')
        )

        # combined score (simple sum) and order by it
        queryset = queryset.annotate(
            score=F('total_plays') + F('total_likes') + F('total_playlist_adds')
        ).order_by('-score', '-total_plays')

        return queryset


class PopularAlbumsView(generics.ListAPIView):
    """Return albums ordered by combined popularity (album likes + song likes + song plays).

    Each album includes the first 3 song cover URLs (ordered by release_date, created_at).
    """
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardResultsSetPagination
    from .serializers import PopularAlbumSerializer
    serializer_class = PopularAlbumSerializer

    def get_permissions(self):
        # Public GET access
        if self.request.method == 'GET':
            return [permissions.AllowAny()]
        return super().get_permissions()

    def get_queryset(self):
        # Annotate album with song-level aggregates
        # Exclude albums literally titled "single" (case-insensitive)
        queryset = Album.objects.all().exclude(title__iexact='single')

        queryset = queryset.annotate(
            total_song_plays=Coalesce(Sum('songs__plays'), 0),
            total_song_likes=Coalesce(Count('songs__liked_by'), 0),
            # album model currently has no direct likes field; set to 0 (no model changes)
            album_likes=Value(0, output_field=IntegerField()),
            playlists_count=Coalesce(Count('songs__playlists'), 0),
            user_playlists_count=Coalesce(Count('songs__user_playlists'), 0),
        ).annotate(
            total_playlist_adds=F('playlists_count') + F('user_playlists_count')
        ).annotate(
            score=F('total_song_plays') + F('total_song_likes') + F('album_likes') + F('total_playlist_adds')
        ).order_by('-score', '-total_song_plays')

        # Prefetch songs ordered so serializer can quickly access first 3 covers
        song_prefetch = Prefetch('songs', queryset=Song.objects.order_by('-release_date', '-created_at'))
        queryset = queryset.prefetch_related(song_prefetch)

        return queryset


class DailyTopSongsView(generics.ListAPIView):
    """Return songs ordered by play count in the last 24 hours (Global)."""
    serializer_class = SongSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        last_24h = timezone.now() - timedelta(hours=24)
        queryset = Song.objects.filter(
            status=Song.STATUS_PUBLISHED
        ).annotate(
            daily_plays=Count('play_counts', filter=Q(play_counts__created_at__gte=last_24h))
        ).filter(
            daily_plays__gt=0
        ).order_by('-daily_plays', '-plays')
        
        return queryset.distinct()


class DailyTopArtistsView(generics.ListAPIView):
    """Return artists ordered by total play count of their songs in the last 24 hours (Global)."""
    serializer_class = PopularArtistSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        last_24h = timezone.now() - timedelta(hours=24)
        queryset = Artist.objects.annotate(
            daily_plays=Count(
                'songs__play_counts', 
                filter=Q(songs__play_counts__created_at__gte=last_24h)
            )
        ).filter(
            daily_plays__gt=0
        ).order_by('-daily_plays')
        
        return queryset.distinct()


class DailyTopAlbumsView(generics.ListAPIView):
    """Return albums ordered by total play count of their songs in the last 24 hours (Global)."""
    serializer_class = PopularAlbumSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        last_24h = timezone.now() - timedelta(hours=24)
        queryset = Album.objects.annotate(
            daily_plays=Count(
                'songs__play_counts', 
                filter=Q(songs__play_counts__created_at__gte=last_24h)
            )
        ).filter(
            daily_plays__gt=0
        ).order_by('-daily_plays')
        
        return queryset.distinct()


class WeeklyTopSongsView(generics.ListAPIView):
    """Return songs ordered by play count in the last 7 days (Global)."""
    serializer_class = SongSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        last_week = timezone.now() - timedelta(days=7)
        # Filter songs that have at least one play in the last week
        # and annotate with the count of plays in that period
        queryset = Song.objects.filter(
            status=Song.STATUS_PUBLISHED
        ).annotate(
            weekly_plays=Count('play_counts', filter=Q(play_counts__created_at__gte=last_week))
        ).filter(
            weekly_plays__gt=0
        ).order_by('-weekly_plays', '-plays')
        
        return queryset.distinct()


class WeeklyTopArtistsView(generics.ListAPIView):
    """Return artists ordered by total play count of their songs in the last 7 days (Global)."""
    serializer_class = PopularArtistSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        last_week = timezone.now() - timedelta(days=7)
        # Filter artists whose songs have at least one play in the last week
        # and annotate with the count of plays in that period
        queryset = Artist.objects.annotate(
            weekly_plays=Count(
                'songs__play_counts', 
                filter=Q(songs__play_counts__created_at__gte=last_week)
            )
        ).filter(
            weekly_plays__gt=0
        ).order_by('-weekly_plays')
        
        return queryset.distinct()


class WeeklyTopAlbumsView(generics.ListAPIView):
    """Return albums ordered by total play count of their songs in the last 7 days (Global)."""
    serializer_class = PopularAlbumSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        last_week = timezone.now() - timedelta(days=7)
        # Filter albums whose songs have at least one play in the last week
        # and annotate with the count of plays in that period
        queryset = Album.objects.annotate(
            weekly_plays=Count(
                'songs__play_counts', 
                filter=Q(songs__play_counts__created_at__gte=last_week)
            )
        ).filter(
            weekly_plays__gt=0
        ).order_by('-weekly_plays')
        
        return queryset.distinct()


class PlaylistRecommendationsView(generics.ListAPIView):
    """
    Auto-generate and return personalized playlist recommendations.
    Based on user activity (plays, likes, playlists).
    Returns existing playlists and generates new ones.
    """
    permission_classes = [IsAuthenticated]
    pagination_class = StandardResultsSetPagination
    from .serializers import RecommendedPlaylistListSerializer
    serializer_class = RecommendedPlaylistListSerializer

    def get_queryset(self):
        user = self.request.user
        
        # Note: generation is triggered in `list()` (every 3 requests).
        # Here we only return cached/available recommendations.
        
        # Return all recommendations for this user, sorted by relevance
        from .models import RecommendedPlaylist
        queryset = RecommendedPlaylist.objects.filter(
            Q(user=user) | Q(user__isnull=True)  # User-specific or general recommendations
        ).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())  # Not expired
        ).prefetch_related('songs', 'liked_by', 'saved_by')
        
        return queryset

    def list(self, request, *args, **kwargs):
        """Override list to count requests and regenerate every 3rd request."""
        user = request.user

        # increment a lightweight counter in user.settings
        try:
            settings_json = user.settings or {}
        except Exception:
            settings_json = {}

        cnt = int(settings_json.get('recommendations_request_count', 0)) + 1
        settings_json['recommendations_request_count'] = cnt
        user.settings = settings_json
        try:
            user.save(update_fields=['settings'])
        except Exception:
            user.save()

        # Regenerate when counter divisible by 3
        if cnt % 3 == 0:
            self._generate_recommendations(user, force=True)

        return super().list(request, *args, **kwargs)


    def _generate_recommendations(self, user, force=False):
        """Generate personalized playlist recommendations based on user activity"""
        from .models import RecommendedPlaylist
        import hashlib
        
        # Check if we have recent recommendations (generated in last 6 hours)
        recent_cutoff = timezone.now() - timedelta(hours=6)
        recent_count = RecommendedPlaylist.objects.filter(
            user=user,
            created_at__gte=recent_cutoff
        ).count()

        # If we have recent recommendations and not forced, skip generation
        if not force and recent_count >= 6:
            return
        
        # 1. Get user's interaction history
        liked_song_ids = set(Song.objects.filter(liked_by=user).values_list('id', flat=True))
        played_song_ids = set(PlayCount.objects.filter(user=user).values_list('songs__id', flat=True))
        playlist_song_ids = set(UserPlaylist.objects.filter(user=user).values_list('songs__id', flat=True))
        
        all_interacted_ids = liked_song_ids | played_song_ids | playlist_song_ids
        
        # If no history, generate general trending playlists
        if not all_interacted_ids:
            self._generate_trending_playlists(user)
            return
        
        # 2. Extract preferences
        interacted_songs = Song.objects.filter(id__in=all_interacted_ids)
        
        # Get top preferences
        top_genres = list(interacted_songs.values_list('genres', flat=True).distinct())[:5]
        top_moods = list(interacted_songs.values_list('moods', flat=True).distinct())[:5]
        top_artists = list(interacted_songs.values_list('artist', flat=True).distinct())[:5]
        
        # Average audio features
        avg_features = interacted_songs.aggregate(
            avg_energy=Avg('energy'),
            avg_dance=Avg('danceability'),
            avg_valence=Avg('valence'),
            avg_tempo=Avg('tempo')
        )
        
        # 3. Generate different types of playlists
        generated_playlists = []
        
        # A. Similar Taste Playlist - Songs close to what they already like
        similar_playlist = self._create_similar_taste_playlist(user, all_interacted_ids, top_genres, top_moods, avg_features)
        if similar_playlist:
            generated_playlists.append(similar_playlist)
        
        # B. Discover Genre Playlists - Explore genres they haven't tried much
        genre_playlists = self._create_genre_discovery_playlists(user, all_interacted_ids, top_genres)
        generated_playlists.extend(genre_playlists)
        
        # C. Mood-Based Playlists
        mood_playlists = self._create_mood_playlists(user, all_interacted_ids, top_moods, avg_features)
        generated_playlists.extend(mood_playlists)
        
        # D. Energy Level Playlists
        energy_playlists = self._create_energy_playlists(user, all_interacted_ids, avg_features)
        generated_playlists.extend(energy_playlists)
        
        # E. Artist Mix Playlists
        artist_playlists = self._create_artist_mix_playlists(user, all_interacted_ids, top_artists)
        generated_playlists.extend(artist_playlists)
        
        # F. If we don't have enough playlists, add general cohesive playlists
        if len(generated_playlists) < 6:
            general_playlists = self._create_general_cohesive_playlists(user, all_interacted_ids, 6 - len(generated_playlists))
            generated_playlists.extend(general_playlists)
        
        # Save all generated playlists (ensure at least 6, max 12)
        for playlist_data in generated_playlists[:12]:
            self._save_playlist(user, playlist_data)

    def _create_similar_taste_playlist(self, user, excluded_ids, top_genres, top_moods, avg_features):
        """Create a playlist similar to user's current taste"""
        candidates = Song.objects.filter(
            status=Song.STATUS_PUBLISHED
        ).exclude(
            id__in=excluded_ids
        ).filter(
            Q(genres__in=top_genres) | Q(moods__in=top_moods)
        ).distinct()[:200]
        
        # Score songs by similarity
        scored_songs = self._score_songs_by_similarity(candidates, top_genres, top_moods, avg_features)
        
        if len(scored_songs) < 10:
            return None
        
        selected_songs = [s[0] for s in scored_songs[:20]]
        
        # Calculate match percentage based on average score
        avg_score = sum(s[1] for s in scored_songs[:20]) / len(scored_songs[:20])
        match_percentage = min(100.0, (avg_score / 20) * 100)  # Normalize to 0-100
        
        import hashlib
        unique_id = hashlib.sha256(f"similar_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
        
        return {
            'unique_id': unique_id,
            'title': 'More of What You Love',
            'description': 'Songs similar to your favorites',
            'playlist_type': 'similar_taste',
            'songs': selected_songs,
            'relevance_score': 10.0,
            'match_percentage': round(match_percentage, 1)
        }

    def _create_genre_discovery_playlists(self, user, excluded_ids, top_genres):
        """Create playlists to help discover new genres"""
        from .models import Genre
        
        playlists = []
        
        # Get genres user hasn't explored much
        all_genres = Genre.objects.exclude(id__in=top_genres)[:3]
        
        for genre in all_genres:
            songs = Song.objects.filter(
                status=Song.STATUS_PUBLISHED,
                genres=genre
            ).exclude(
                id__in=excluded_ids
            ).order_by('-plays')[:20]
            
            if songs.count() >= 10:
                import hashlib
                unique_id = hashlib.sha256(f"discover_{genre.id}_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
                
                playlists.append({
                    'unique_id': unique_id,
                    'title': f'Discover {genre.name}',
                    'description': f'Explore popular {genre.name} tracks',
                    'playlist_type': 'discover_genre',
                    'songs': list(songs),
                    'relevance_score': 7.0,
                    'match_percentage': 0.0  # Discovery playlists don't match existing taste
                })
        
        return playlists[:3]  # Max 3 discovery playlists

    def _create_mood_playlists(self, user, excluded_ids, top_moods, avg_features):
        """Create mood-based playlists"""
        from .models import Mood
        
        playlists = []
        
        # Get user's favorite moods
        moods = Mood.objects.filter(id__in=top_moods)[:2]
        
        for mood in moods:
            songs = Song.objects.filter(
                status=Song.STATUS_PUBLISHED,
                moods=mood
            ).exclude(
                id__in=excluded_ids
            )
            
            # Filter by similar audio features
            if avg_features['avg_valence']:
                songs = songs.filter(
                    valence__gte=avg_features['avg_valence'] - 20,
                    valence__lte=avg_features['avg_valence'] + 20
                )
            
            songs = songs.order_by('-plays')[:20]
            
            if songs.count() >= 10:
                import hashlib
                unique_id = hashlib.sha256(f"mood_{mood.id}_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
                
                playlists.append({
                    'unique_id': unique_id,
                    'title': f'{mood.name} Vibes',
                    'description': f'Perfect songs for a {mood.name.lower()} mood',
                    'playlist_type': 'mood_based',
                    'songs': list(songs),
                    'relevance_score': 8.5,
                    'match_percentage': 75.0  # Mood-based matches user's mood preferences
                })
        
        return playlists

    def _create_energy_playlists(self, user, excluded_ids, avg_features):
        """Create playlists based on energy levels"""
        playlists = []
        
        if not avg_features['avg_energy']:
            return playlists
        
        # High energy playlist
        high_energy_songs = Song.objects.filter(
            status=Song.STATUS_PUBLISHED,
            energy__gte=70
        ).exclude(
            id__in=excluded_ids
        ).order_by('-energy', '-plays')[:20]
        
        if high_energy_songs.count() >= 10:
            import hashlib
            unique_id = hashlib.sha256(f"high_energy_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
            
            playlists.append({
                'unique_id': unique_id,
                'title': 'High Energy Boost',
                'description': 'Pump up the energy with these tracks',
                'playlist_type': 'energy',
                'songs': list(high_energy_songs),
                'relevance_score': 7.5,
                'match_percentage': 60.0  # Energy-based has moderate match
            })
        
        # Chill/Relaxing playlist
        chill_songs = Song.objects.filter(
            status=Song.STATUS_PUBLISHED,
            energy__lte=40,
            acousticness__gte=30
        ).exclude(
            id__in=excluded_ids
        ).order_by('energy', '-plays')[:20]
        
        if chill_songs.count() >= 10:
            import hashlib
            unique_id = hashlib.sha256(f"chill_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
            
            playlists.append({
                'unique_id': unique_id,
                'title': 'Chill & Relax',
                'description': 'Unwind with these mellow tracks',
                'playlist_type': 'energy',
                'songs': list(chill_songs),
                'relevance_score': 7.5,
                'match_percentage': 60.0  # Energy-based has moderate match
            })
        
        return playlists  # Return all energy playlists (up to 2)

    def _create_artist_mix_playlists(self, user, excluded_ids, top_artists):
        """Create playlists mixing songs from favorite artists"""
        from .models import Artist
        
        playlists = []
        
        if len(top_artists) < 2:
            return playlists
        
        # Get songs from top 3 artists
        artists = Artist.objects.filter(id__in=top_artists[:3])
        
        songs = Song.objects.filter(
            status=Song.STATUS_PUBLISHED,
            artist__in=artists
        ).exclude(
            id__in=excluded_ids
        ).order_by('-plays', '-release_date')[:20]
        
        if songs.count() >= 10:
            artist_names = ', '.join([a.name for a in artists[:2]])
            import hashlib
            unique_id = hashlib.sha256(f"artist_mix_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
            
            playlists.append({
                'unique_id': unique_id,
                'title': f'{artist_names} & More',
                'description': f'A mix featuring your favorite artists',
                'playlist_type': 'artist_mix',
                'songs': list(songs),
                'relevance_score': 9.0,
                'match_percentage': 85.0  # Artist mix has high match with user taste
            })
        
        return playlists

    def _generate_trending_playlists(self, user):
        """Generate general trending playlists for users without history"""
        import hashlib
        
        playlists = []
        
        # Trending overall
        trending_songs = Song.objects.filter(
            status=Song.STATUS_PUBLISHED
        ).order_by('-plays')[:20]
        
        if trending_songs.count() >= 10:
            unique_id = hashlib.sha256(f"trending_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
            playlists.append({
                'unique_id': unique_id,
                'title': 'Trending Now',
                'description': 'Most popular tracks right now',
                'playlist_type': 'similar_taste',
                'songs': list(trending_songs),
                'relevance_score': 5.0,
                'match_percentage': 0.0
            })
        
        # New releases
        new_releases = Song.objects.filter(
            status=Song.STATUS_PUBLISHED,
            release_date__isnull=False
        ).order_by('-release_date')[:20]
        
        if new_releases.count() >= 10:
            unique_id = hashlib.sha256(f"new_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
            playlists.append({
                'unique_id': unique_id,
                'title': 'Fresh Releases',
                'description': 'Newest tracks just for you',
                'playlist_type': 'similar_taste',
                'songs': list(new_releases),
                'relevance_score': 5.0,
                'match_percentage': 0.0
            })
        
        # Add general cohesive playlists to reach at least 6
        if len(playlists) < 6:
            general_playlists = self._create_general_cohesive_playlists(user, set(), 6 - len(playlists))
            playlists.extend(general_playlists)
        
        # Save all playlists
        for playlist_data in playlists:
            self._save_playlist(user, playlist_data)

    def _create_general_cohesive_playlists(self, user, excluded_ids, count_needed):
        """Create general playlists that are cohesive but not user-specific"""
        from .models import Genre, Mood
        import hashlib
        
        playlists = []
        
        # 1. High Energy Workout
        if count_needed > 0:
            high_energy = Song.objects.filter(
                status=Song.STATUS_PUBLISHED,
                energy__gte=75,
                danceability__gte=60
            ).exclude(id__in=excluded_ids).order_by('-plays')[:20]
            
            if high_energy.count() >= 10:
                unique_id = hashlib.sha256(f"gen_energy_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
                playlists.append({
                    'unique_id': unique_id,
                    'title': 'Power Workout',
                    'description': 'High-energy tracks to fuel your workout',
                    'playlist_type': 'energy',
                    'songs': list(high_energy),
                    'relevance_score': 6.0,
                    'match_percentage': 0.0
                })
        
        # 2. Acoustic & Chill
        if len(playlists) < count_needed:
            acoustic = Song.objects.filter(
                status=Song.STATUS_PUBLISHED,
                acousticness__gte=60,
                energy__lte=50
            ).exclude(id__in=excluded_ids).order_by('-plays')[:20]
            
            if acoustic.count() >= 10:
                unique_id = hashlib.sha256(f"gen_acoustic_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
                playlists.append({
                    'unique_id': unique_id,
                    'title': 'Acoustic Sessions',
                    'description': 'Mellow acoustic vibes for relaxation',
                    'playlist_type': 'mood_based',
                    'songs': list(acoustic),
                    'relevance_score': 6.0,
                    'match_percentage': 0.0
                })
        
        # 3. Happy & Upbeat
        if len(playlists) < count_needed:
            happy = Song.objects.filter(
                status=Song.STATUS_PUBLISHED,
                valence__gte=70,
                energy__gte=60
            ).exclude(id__in=excluded_ids).order_by('-plays')[:20]
            
            if happy.count() >= 10:
                unique_id = hashlib.sha256(f"gen_happy_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
                playlists.append({
                    'unique_id': unique_id,
                    'title': 'Feel Good Hits',
                    'description': 'Upbeat songs to brighten your day',
                    'playlist_type': 'mood_based',
                    'songs': list(happy),
                    'relevance_score': 6.0,
                    'match_percentage': 0.0
                })
        
        # 4. Melancholic & Dramatic
        if len(playlists) < count_needed:
            dramatic = Song.objects.filter(
                status=Song.STATUS_PUBLISHED,
                valence__lte=40,
                energy__lte=60
            ).exclude(id__in=excluded_ids).order_by('-plays')[:20]
            
            if dramatic.count() >= 10:
                unique_id = hashlib.sha256(f"gen_dramatic_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
                playlists.append({
                    'unique_id': unique_id,
                    'title': 'Emotional Journey',
                    'description': 'Deep, emotional tracks for reflective moments',
                    'playlist_type': 'mood_based',
                    'songs': list(dramatic),
                    'relevance_score': 6.0,
                    'match_percentage': 0.0
                })
        
        # 5. Dance Party
        if len(playlists) < count_needed:
            dance = Song.objects.filter(
                status=Song.STATUS_PUBLISHED,
                danceability__gte=75
            ).exclude(id__in=excluded_ids).order_by('-plays')[:20]
            
            if dance.count() >= 10:
                unique_id = hashlib.sha256(f"gen_dance_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
                playlists.append({
                    'unique_id': unique_id,
                    'title': 'Dance Floor Fillers',
                    'description': 'Get moving with these danceable tracks',
                    'playlist_type': 'energy',
                    'songs': list(dance),
                    'relevance_score': 6.0,
                    'match_percentage': 0.0
                })
        
        # 6. Focus & Study
        if len(playlists) < count_needed:
            focus = Song.objects.filter(
                status=Song.STATUS_PUBLISHED,
                instrumentalness__gte=50,
                energy__lte=50
            ).exclude(id__in=excluded_ids).order_by('-plays')[:20]
            
            if focus.count() >= 10:
                unique_id = hashlib.sha256(f"gen_focus_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
                playlists.append({
                    'unique_id': unique_id,
                    'title': 'Focus & Productivity',
                    'description': 'Instrumental tracks for concentration',
                    'playlist_type': 'mood_based',
                    'songs': list(focus),
                    'relevance_score': 6.0,
                    'match_percentage': 0.0
                })
        
        # 7. Late Night Vibes
        if len(playlists) < count_needed:
            night = Song.objects.filter(
                status=Song.STATUS_PUBLISHED,
                energy__lte=45,
                valence__range=(30, 60)
            ).exclude(id__in=excluded_ids).order_by('-plays')[:20]
            
            if night.count() >= 10:
                unique_id = hashlib.sha256(f"gen_night_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
                playlists.append({
                    'unique_id': unique_id,
                    'title': 'Late Night Vibes',
                    'description': 'Smooth tracks for the evening',
                    'playlist_type': 'mood_based',
                    'songs': list(night),
                    'relevance_score': 6.0,
                    'match_percentage': 0.0
                })
        
        # 8. Genre-based fallbacks
        if len(playlists) < count_needed:
            all_genres = Genre.objects.all()[:5]
            for genre in all_genres:
                if len(playlists) >= count_needed:
                    break
                    
                genre_songs = Song.objects.filter(
                    status=Song.STATUS_PUBLISHED,
                    genres=genre
                ).exclude(id__in=excluded_ids).order_by('-plays')[:20]
                
                if genre_songs.count() >= 10:
                    unique_id = hashlib.sha256(f"gen_genre_{genre.id}_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
                    playlists.append({
                        'unique_id': unique_id,
                        'title': f'Best of {genre.name}',
                        'description': f'Top {genre.name} tracks',
                        'playlist_type': 'discover_genre',
                        'songs': list(genre_songs),
                        'relevance_score': 5.5,
                        'match_percentage': 0.0
                    })
        
        return playlists[:count_needed]

    def _score_songs_by_similarity(self, songs, top_genres, top_moods, avg_features):
        """Score songs by similarity to user preferences"""
        scored = []
        
        for song in songs:
            score = 0
            
            # Genre matching
            song_genres = set(song.genres.values_list('id', flat=True))
            score += len(song_genres.intersection(top_genres)) * 3
            
            # Mood matching
            song_moods = set(song.moods.values_list('id', flat=True))
            score += len(song_moods.intersection(top_moods)) * 2
            
            # Audio feature similarity
            if avg_features['avg_energy'] and song.energy:
                score += max(0, (100 - abs(song.energy - avg_features['avg_energy'])) / 10)
            if avg_features['avg_dance'] and song.danceability:
                score += max(0, (100 - abs(song.danceability - avg_features['avg_dance'])) / 10)
            if avg_features['avg_valence'] and song.valence:
                score += max(0, (100 - abs(song.valence - avg_features['avg_valence'])) / 10)
            
            # Popularity boost (but not too much)
            score += min(song.plays / 1000, 5)
            
            scored.append((song, score))
        
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def _save_playlist(self, user, playlist_data):
        """Save or update a recommended playlist"""
        from .models import RecommendedPlaylist, Playlist
        
        # Check if playlist with this unique_id already exists
        existing = RecommendedPlaylist.objects.filter(
            unique_id=playlist_data['unique_id']
        ).first()
        
        # Create or update the canonical Playlist object
        if existing and existing.playlist_ref:
            pl = existing.playlist_ref
            pl.title = playlist_data['title']
            pl.description = playlist_data['description']
            pl.save()
        else:
            pl = Playlist.objects.create(
                title=playlist_data['title'],
                description=playlist_data['description'],
                created_by=Playlist.CREATED_BY_SYSTEM
            )
        
        # Sync songs to the canonical playlist
        pl.songs.set(playlist_data['songs'])
        
        if existing:
            # Update existing
            existing.title = playlist_data['title']
            existing.description = playlist_data['description']
            existing.relevance_score = playlist_data['relevance_score']
            existing.match_percentage = playlist_data.get('match_percentage', 0.0)
            existing.updated_at = timezone.now()
            existing.expires_at = timezone.now() + timedelta(days=7)
            existing.playlist_ref = pl
            # Update songs: clear/add then set a randomized explicit order
            existing.songs.clear()
            existing.songs.add(*playlist_data['songs'])
            try:
                song_ids = [s.id for s in playlist_data['songs']]
            except Exception:
                song_ids = []
            # Shuffle to ensure list view doesn't look identical each time
            if song_ids:
                random.shuffle(song_ids)
                existing.song_order = song_ids
            existing.save()
        else:
            # Create new
            playlist = RecommendedPlaylist.objects.create(
                unique_id=playlist_data['unique_id'],
                user=user,
                title=playlist_data['title'],
                description=playlist_data['description'],
                playlist_type=playlist_data['playlist_type'],
                relevance_score=playlist_data['relevance_score'],
                match_percentage=playlist_data.get('match_percentage', 0.0),
                expires_at=timezone.now() + timedelta(days=7),
                playlist_ref=pl
            )
            playlist.songs.add(*playlist_data['songs'])
            try:
                song_ids = [s.id for s in playlist_data['songs']]
            except Exception:
                song_ids = []
            if song_ids:
                random.shuffle(song_ids)
                playlist.song_order = song_ids
                playlist.save()


class PlaylistRecommendationDetailView(generics.RetrieveAPIView):
    """
    Detail view for a specific recommended playlist.
    Shows all songs without stream links.
    Increments view count.
    """
    permission_classes = [IsAuthenticated]
    from .serializers import RecommendedPlaylistDetailSerializer
    serializer_class = RecommendedPlaylistDetailSerializer
    lookup_field = 'unique_id'

    def get_queryset(self):
        from .models import RecommendedPlaylist
        return RecommendedPlaylist.objects.all().prefetch_related(
            'songs__artist',
            'songs__album',
            'liked_by',
            'saved_by'
        )

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        
        # Increment view count and record user
        instance.views += 1
        if request.user.is_authenticated:
            instance.viewed_by.add(request.user)
        instance.save(update_fields=['views'])
        
        serializer = self.get_serializer(instance)
        return Response(serializer.data)


class PlaylistRecommendationLikeView(APIView):
    """Like or unlike a recommended playlist"""
    permission_classes = [IsAuthenticated]

    def post(self, request, unique_id):
        from .models import RecommendedPlaylist
        
        try:
            playlist = RecommendedPlaylist.objects.get(unique_id=unique_id)
        except RecommendedPlaylist.DoesNotExist:
            return Response(
                {'error': 'Playlist not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        if playlist.liked_by.filter(id=request.user.id).exists():
            # Unlike
            playlist.liked_by.remove(request.user)
            return Response({'status': 'unliked', 'likes_count': playlist.liked_by.count()})
        else:
            # Like
            playlist.liked_by.add(request.user)
            return Response({'status': 'liked', 'likes_count': playlist.liked_by.count()})


class PlaylistRecommendationSaveView(APIView):
    """Save or unsave a recommended playlist"""
    permission_classes = [IsAuthenticated]

    def post(self, request, unique_id):
        from .models import RecommendedPlaylist
        
        try:
            playlist = RecommendedPlaylist.objects.get(unique_id=unique_id)
        except RecommendedPlaylist.DoesNotExist:
            return Response(
                {'error': 'Playlist not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        if playlist.saved_by.filter(id=request.user.id).exists():
            # Unsave
            playlist.saved_by.remove(request.user)
            return Response({'status': 'unsaved'})
        else:
            # Save
            playlist.saved_by.add(request.user)
            return Response({'status': 'saved'})


class PlaylistSaveToggleView(APIView):
    """Toggle save/unsave for canonical Playlist objects"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk, *args, **kwargs):
        try:
            playlist = Playlist.objects.get(id=pk)
        except Playlist.DoesNotExist:
            return Response({'detail': 'playlist not found'}, status=status.HTTP_404_NOT_FOUND)

        user = request.user
        if playlist.saved_by.filter(id=user.id).exists():
            playlist.saved_by.remove(user)
            return Response({'status': 'unsaved'}, status=status.HTTP_200_OK)
        else:
            playlist.saved_by.add(user)
            return Response({'status': 'saved'}, status=status.HTTP_200_OK)


class SearchView(APIView):
    """
    Unified search endpoint for songs, artists, albums, and playlists.
    Supports complex matching and mixed results.
    """
    permission_classes = [AllowAny]
    
    def get(self, request):
        q = request.query_params.get('q', '').strip()
        search_type = request.query_params.get('type')
        moods = request.query_params.getlist('moods')
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))
        
        offset = (page - 1) * page_size
        results = []
        
        if search_type:
            # Single type search
            if search_type == 'song':
                items = self._search_songs(q, moods)
            elif search_type == 'artist':
                items = self._search_artists(q)
            elif search_type == 'album':
                items = self._search_albums(q)
            elif search_type == 'playlist':
                items = self._search_playlists(q, moods)
            else:
                return Response({'error': 'Invalid type. Must be song, artist, album, or playlist.'}, status=400)
            
            paginated_items = items[offset:offset + page_size]
            results = list(paginated_items)
            has_next = items.count() > offset + page_size
        else:
            # Mixed search (interleaved)
            per_type = page_size // 4
            
            songs = list(self._search_songs(q, moods)[offset:offset + per_type])
            artists = list(self._search_artists(q)[offset:offset + per_type])
            albums = list(self._search_albums(q)[offset:offset + per_type])
            playlists = list(self._search_playlists(q, moods)[offset:offset + per_type])
            
            # Interleave results
            max_len = max(len(songs), len(artists), len(albums), len(playlists))
            for i in range(max_len):
                if i < len(songs): results.append(songs[i])
                if i < len(artists): results.append(artists[i])
                if i < len(albums): results.append(albums[i])
                if i < len(playlists): results.append(playlists[i])
            
            # For mixed, we assume there's more if we got a full page
            has_next = len(results) >= page_size

        serializer = SearchResultSerializer(results, many=True, context={'request': request})
        
        return Response({
            'results': serializer.data,
            'page': page,
            'page_size': page_size,
            'has_next': has_next,
            'query': q,
            'moods': moods,
            'type': search_type or 'mixed'
        })

    def _search_songs(self, q, moods=None):
        qs = Song.objects.filter(status=Song.STATUS_PUBLISHED).select_related('artist', 'album')
        if q:
            # Complex matching: title, description, lyrics, producers, composers, lyricists, artist name, album title
            qs = qs.filter(
                Q(title__icontains=q) |
                Q(description__icontains=q) |
                Q(lyrics__icontains=q) |
                Q(producers__icontains=q) |
                Q(composers__icontains=q) |
                Q(lyricists__icontains=q) |
                Q(artist__name__icontains=q) |
                Q(album__title__icontains=q)
            )
        
        if moods:
            # Filter by mood IDs or slugs
            if all(m.isdigit() for m in moods):
                qs = qs.filter(moods__id__in=moods).distinct()
            else:
                qs = qs.filter(moods__slug__in=moods).distinct()
                
        return qs.order_by('-plays', '-created_at')

    def _search_artists(self, q):
        qs = Artist.objects.all()
        if q:
            qs = qs.filter(
                Q(name__icontains=q) |
                Q(bio__icontains=q)
            )
        return qs.order_by('-verified', '-created_at')

    def _search_albums(self, q):
        qs = Album.objects.all().select_related('artist')
        if q:
            qs = qs.filter(
                Q(title__icontains=q) |
                Q(description__icontains=q) |
                Q(artist__name__icontains=q)
            )
        return qs.order_by('-release_date')

    def _search_playlists(self, q, moods=None):
        # Combine admin/system playlists and public user playlists
        admin_qs = Playlist.objects.all()
        if q:
            admin_qs = admin_qs.filter(
                Q(title__icontains=q) |
                Q(description__icontains=q)
            )
        
        if moods:
            if all(m.isdigit() for m in moods):
                admin_qs = admin_qs.filter(moods__id__in=moods).distinct()
            else:
                admin_qs = admin_qs.filter(moods__slug__in=moods).distinct()
                
        return admin_qs.order_by('-created_at')


class EventPlaylistView(APIView):
    """Return event playlist groups with all details."""
    permission_classes = [permissions.AllowAny]
    
    def get(self, request):
        queryset = EventPlaylist.objects.all().prefetch_related(
            'playlists', 
            'playlists__songs',
            'playlists__songs__artist',
            'playlists__genres',
            'playlists__moods'
        )
        
        time_of_day = request.query_params.get('time_of_day')
        if time_of_day:
            queryset = queryset.filter(time_of_day=time_of_day)
            
        serializer = EventPlaylistSerializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)


class SearchSectionListView(APIView):
    """List and Create SearchSections"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get(self, request):
        sections = SearchSection.objects.all().prefetch_related('songs', 'albums', 'playlists', 'songs__artist', 'albums__artist')
        serializer = SearchSectionSerializer(sections, many=True, context={'request': request})
        return Response(serializer.data)

    def post(self, request):
        serializer = SearchSectionSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save(created_by=request.user, updated_by=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SearchSectionDetailView(APIView):
    """Retrieve, Update, and Delete SearchSection"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_object(self, pk):
        try:
            return SearchSection.objects.get(pk=pk)
        except SearchSection.DoesNotExist:
            return None

    def get(self, request, pk):
        section = self.get_object(pk)
        if not section:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = SearchSectionSerializer(section, context={'request': request})
        return Response(serializer.data)

    def put(self, request, pk):
        section = self.get_object(pk)
        if not section:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = SearchSectionSerializer(section, data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save(updated_by=request.user)
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request, pk):
        section = self.get_object(pk)
        if not section:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = SearchSectionSerializer(section, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save(updated_by=request.user)
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        section = self.get_object(pk)
        if not section:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        section.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class RulesListCreateView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        rules = Rules.objects.all()
        serializer = RulesSerializer(rules, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = RulesSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class RulesDetailView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get_object(self, pk):
        try:
            return Rules.objects.get(pk=pk)
        except Rules.DoesNotExist:
            return None

    def get(self, request, pk):
        rule = self.get_object(pk)
        if not rule:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = RulesSerializer(rule)
        return Response(serializer.data)

    def put(self, request, pk):
        rule = self.get_object(pk)
        if not rule:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = RulesSerializer(rule, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request, pk):
        rule = self.get_object(pk)
        if not rule:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = RulesSerializer(rule, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class PlayConfigurationView(APIView):
    """View to get or update the play configuration. Only one record should exist."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        config = PlayConfiguration.objects.last()
        if not config:
            config = PlayConfiguration.objects.create()
        serializer = PlayConfigurationSerializer(config)
        return Response(serializer.data)

    def post(self, request):
        if not request.user.is_staff:
            return Response({'error': 'Only staff can update configuration'}, status=status.HTTP_403_FORBIDDEN)
        
        config = PlayConfiguration.objects.last()
        if not config:
            config = PlayConfiguration.objects.create()
        
        serializer = PlayConfigurationSerializer(config, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        rule = self.get_object(pk)
        if not rule:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        rule.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ArtistHomeView(APIView):
    """
    Artist Dashboard Home Endpoint.
    Provides income summary, play counts, daily play details, and top songs.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        # Check if user has artist role
        if user.roles != User.ROLE_ARTIST:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)
        
        try:
            artist = user.artist_profile
        except Artist.DoesNotExist:
            return Response({"error": "Artist profile not found"}, status=status.HTTP_404_NOT_FOUND)

        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_start = today_start - timedelta(days=1)
        
        last_7d_start = today_start - timedelta(days=7)
        prev_7d_start = last_7d_start - timedelta(days=7)
        
        last_30d_start = today_start - timedelta(days=30)
        prev_30d_start = last_30d_start - timedelta(days=30)

        def get_stats(start_date, end_date=None):
            qs = PlayCount.objects.filter(songs__artist=artist, created_at__gte=start_date)
            if end_date:
                qs = qs.filter(created_at__lt=end_date)
            
            stats = qs.aggregate(
                total_income=Coalesce(Sum('pay'), Value(0, output_field=DecimalField(max_digits=10, decimal_places=6))),
                total_plays=Count('id')
            )
            return stats

        def format_growth(current, previous):
            if not previous or previous == 0:
                return None
            growth = ((float(current) - float(previous)) / float(previous)) * 100
            if growth >= 0:
                return f"{growth:.1f}%+"
            else:
                return f"{abs(growth):.1f}%-"

        # Stats
        today_stats = get_stats(today_start)
        yesterday_stats = get_stats(yesterday_start, today_start)
        
        last_7d_stats = get_stats(last_7d_start)
        prev_7d_stats = get_stats(prev_7d_start, last_7d_start)
        
        last_30d_stats = get_stats(last_30d_start)
        prev_30d_stats = get_stats(prev_30d_start, last_30d_start)

        # Income Summary
        income_summary = {
            "today": today_stats['total_income'],
            "last_7_days": last_7d_stats['total_income'],
            "last_30_days": last_30d_stats['total_income'],
            "growth": {
                "today": format_growth(today_stats['total_income'], yesterday_stats['total_income']),
                "last_7_days": format_growth(last_7d_stats['total_income'], prev_7d_stats['total_income']),
                "last_30_days": format_growth(last_30d_stats['total_income'], prev_30d_stats['total_income']),
            }
        }

        # Play Counts Summary
        plays_summary = {
            "today": today_stats['total_plays'],
            "last_7_days": last_7d_stats['total_plays'],
            "last_30_days": last_30d_stats['total_plays'],
            "growth": {
                "today": format_growth(today_stats['total_plays'], yesterday_stats['total_plays']),
                "last_7_days": format_growth(last_7d_stats['total_plays'], prev_7d_stats['total_plays']),
                "last_30_days": format_growth(last_30d_stats['total_plays'], prev_30d_stats['total_plays']),
            }
        }

        # Daily plays for last 7 days (including today)
        daily_plays = []
        for i in range(7):
            day_start = today_start - timedelta(days=i)
            day_end = day_start + timedelta(days=1)
            count = PlayCount.objects.filter(songs__artist=artist, created_at__gte=day_start, created_at__lt=day_end).count()
            daily_plays.append({
                "date": day_start.date().isoformat(),
                "count": count
            })
        daily_plays.reverse()

        # Top 6 songs
        top_songs_qs = Song.objects.filter(artist=artist).annotate(
            total_plays_calc=F('plays') + Count('play_counts')
        ).order_by('-total_plays_calc')[:6]
        
        top_songs = SongSerializer(top_songs_qs, many=True, context={'request': request}).data

        return Response({
            "income_summary": income_summary,
            "plays_summary": plays_summary,
            "daily_plays": daily_plays,
            "top_songs": top_songs
        })


class ArtistLiveListenersView(APIView):
    """
    Retrieve the current live listener count for the authenticated artist.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if user.roles != User.ROLE_ARTIST:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)
        
        try:
            artist = user.artist_profile
        except Artist.DoesNotExist:
            return Response({"error": "Artist profile not found"}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            "artist_id": artist.id,
            "artist_name": artist.name,
            "live_listeners": artist.live_listeners
        })


class ArtistLiveListenersPollView(APIView):
    """
    Long-polling endpoint for live listener updates.
    Blocks until the set of live listeners changes or a timeout occurs.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if user.roles != User.ROLE_ARTIST:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)
        
        try:
            artist = user.artist_profile
        except Artist.DoesNotExist:
            return Response({"error": "Artist profile not found"}, status=status.HTTP_404_NOT_FOUND)

        def get_current_listeners():
            return set(ActivePlayback.objects.filter(
                song__artist=artist,
                expiration_time__gt=timezone.now()
            ).values_list('user_id', flat=True).distinct())

        initial_listeners = get_current_listeners()
        
        # Long polling loop
        timeout = 30  # seconds
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            current_listeners = get_current_listeners()
            if current_listeners != initial_listeners:
                return Response({
                    "live_listeners": len(current_listeners),
                    "changed": True
                })
            time.sleep(3)  # Check every 3 seconds
            
        return Response({
            "live_listeners": len(initial_listeners),
            "changed": False
        })
