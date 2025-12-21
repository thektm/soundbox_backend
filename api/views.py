from rest_framework import generics, permissions, viewsets
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.decorators import action
from .models import User, Artist, Album, Genre, Mood, Tag, SubGenre, Song, StreamAccess, PlayCount, UserPlaylist
from .serializers import (
    UserSerializer,
    RegisterSerializer,
    CustomTokenObtainPairSerializer,
    ArtistSerializer,
    AlbumSerializer,
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
)
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework.parsers import MultiPartParser, FormParser
from django.conf import settings
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import uuid
import os
import mimetypes
from mutagen.mp3 import MP3
from mutagen.wave import WAVE
from django.utils import timezone
from datetime import timedelta
from django.db.models import Q, Count, Avg, F


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


class UserProfileView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user


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


class ArtistViewSet(viewsets.ModelViewSet):
    """ViewSet for Artist CRUD operations"""
    queryset = Artist.objects.all()
    serializer_class = ArtistSerializer
    permission_classes = [IsAuthenticated]
    
    def get_permissions(self):
        if self.action == 'list' or self.action == 'retrieve':
            return [AllowAny()]
        return super().get_permissions()


class AlbumViewSet(viewsets.ModelViewSet):
    """ViewSet for Album CRUD operations"""
    queryset = Album.objects.all()
    serializer_class = AlbumSerializer
    permission_classes = [IsAuthenticated]
    
    def get_permissions(self):
        if self.action == 'list' or self.action == 'retrieve':
            return [AllowAny()]
        return super().get_permissions()


class GenreViewSet(viewsets.ModelViewSet):
    """ViewSet for Genre CRUD operations"""
    queryset = Genre.objects.all()
    serializer_class = GenreSerializer
    permission_classes = [IsAuthenticated]
    
    def get_permissions(self):
        if self.action == 'list' or self.action == 'retrieve':
            return [AllowAny()]
        return super().get_permissions()


class MoodViewSet(viewsets.ModelViewSet):
    """ViewSet for Mood CRUD operations"""
    queryset = Mood.objects.all()
    serializer_class = MoodSerializer
    permission_classes = [IsAuthenticated]
    
    def get_permissions(self):
        if self.action == 'list' or self.action == 'retrieve':
            return [AllowAny()]
        return super().get_permissions()


class TagViewSet(viewsets.ModelViewSet):
    """ViewSet for Tag CRUD operations"""
    queryset = Tag.objects.all()
    serializer_class = TagSerializer
    permission_classes = [IsAuthenticated]
    
    def get_permissions(self):
        if self.action == 'list' or self.action == 'retrieve':
            return [AllowAny()]
        return super().get_permissions()


class SubGenreViewSet(viewsets.ModelViewSet):
    """ViewSet for SubGenre CRUD operations"""
    queryset = SubGenre.objects.all()
    serializer_class = SubGenreSerializer
    permission_classes = [IsAuthenticated]
    
    def get_permissions(self):
        if self.action == 'list' or self.action == 'retrieve':
            return [AllowAny()]
        return super().get_permissions()


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


class SongDetailView(generics.RetrieveUpdateDestroyAPIView):
    """View for retrieving, updating and deleting a song"""
    queryset = Song.objects.all()
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


class SongLikeView(APIView):
    """Toggle like status for a song"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk=None):
        try:
            song = Song.objects.get(pk=pk)
        except Song.DoesNotExist:
            return Response({'error': 'Song not found'}, status=status.HTTP_404_NOT_FOUND)
            
        user = request.user
        if song.liked_by.filter(id=user.id).exists():
            song.liked_by.remove(user)
            liked = False
        else:
            song.liked_by.add(user)
            liked = True
            
        return Response({
            'liked': liked,
            'likes_count': song.liked_by.count()
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

            play_count = PlayCount.objects.create(
                user=request.user,
                country=country,
                city=city,
                ip=ip
            )
            song.play_counts.add(play_count)

            # Mark as used
            stream_access.one_time_used = True
            stream_access.save(update_fields=['one_time_used'])

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
        
        genre_ids = [g['genres'] for g in top_genres if g['genres']]
        mood_ids = [m['moods'] for m in top_moods if m['moods']]
        artist_ids = [a['artist'] for a in top_artists if a['artist']]
        
        # Average audio features
        avg_features = interacted_songs.aggregate(
            avg_energy=Avg('energy'),
            avg_dance=Avg('danceability'),
            avg_valence=Avg('valence'),
            avg_tempo=Avg('tempo')
        )

        # 3. Candidate Generation
        # Find songs that match top genres, moods, or artists but haven't been interacted with
        candidates = Song.objects.filter(
            status=Song.STATUS_PUBLISHED
        ).exclude(
            id__in=all_interacted_ids
        ).filter(
            Q(genres__in=genre_ids) | 
            Q(moods__in=mood_ids) | 
            Q(artist__in=artist_ids)
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
