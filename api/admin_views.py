from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from rest_framework.pagination import PageNumberPagination
from django.shortcuts import get_object_or_404
from .models import (
    User, Artist, ArtistAuth, Song, Album, Genre, SubGenre, Mood, Tag, Report, 
    PlayConfiguration, BannerAd, AudioAd, PaymentTransaction, DepositRequest,
    SearchSection, EventPlaylist, Playlist
)
from .models import PlayCount
from django.utils import timezone
from datetime import timedelta
from django.db.models import Sum, Count, Q
from decimal import Decimal
from .admin_serializers import (
    AdminUserSerializer, AdminArtistSerializer, AdminArtistAuthSerializer, 
    AdminSongSerializer, AdminReportSerializer, AdminAlbumSerializer,
    AdminPlayConfigurationSerializer, AdminBannerAdSerializer, AdminAudioAdSerializer,
    AdminPaymentTransactionSerializer, AdminDepositRequestSerializer,
    AdminSearchSectionSerializer, AdminEventPlaylistSerializer, AdminPlaylistSerializer
)
from rest_framework.parsers import MultiPartParser, FormParser
from .utils import upload_file_to_r2, convert_to_128kbps, get_audio_info
import os

class AdminPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100

class AdminUserListView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        # Default: audience only, sorted by join date latest first
        users = User.objects.filter(roles__contains=User.ROLE_AUDIENCE).order_by('-date_joined')
        
        # Optional filtering by role if needed in future
        role = request.query_params.get('role')
        if role:
            users = User.objects.filter(roles__contains=role).order_by('-date_joined')

        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(users, request)
        serializer = AdminUserSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

class AdminUserDetailView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        serializer = AdminUserSerializer(user)
        return Response(serializer.data)

    def put(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        serializer = AdminUserSerializer(user, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        serializer = AdminUserSerializer(user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminUserBanView(APIView):
    """Ban a user and delete their artist profile and content."""
    permission_classes = [permissions.IsAdminUser]

    def post(self, request):
        user_id = request.data.get('user_id')
        if not user_id:
            return Response({"error": "user_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        user = get_object_or_404(User, pk=user_id)
        
        # 1. Mark as banned and inactive
        user.is_banned = True
        user.is_active = False
        user.save()
        
        # 2. Delete artist profile if exists
        if hasattr(user, 'artist_profile'):
            artist = user.artist_profile
            # This will cascade delete songs and albums
            artist.delete()
            
        # 3. Delete user's playlists
        user.user_playlists.all().delete()
        
        return Response({"message": f"User {user.phone_number} has been banned and their content deleted."})

class AdminArtistListView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        artists = Artist.objects.all().order_by('-created_at')
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(artists, request)
        serializer = AdminArtistSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

class AdminArtistDetailView(APIView):
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request, pk):
        artist = get_object_or_404(Artist, pk=pk)
        serializer = AdminArtistSerializer(artist)
        return Response(serializer.data)

    def put(self, request, pk):
        artist = get_object_or_404(Artist, pk=pk)
        serializer = AdminArtistSerializer(artist, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request, pk):
        artist = get_object_or_404(Artist, pk=pk)
        serializer = AdminArtistSerializer(artist, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        artist = get_object_or_404(Artist, pk=pk)
        artist.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

class AdminPendingArtistListView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        # records of artistAuth with not accepted or rejected status
        pending_auths = ArtistAuth.objects.exclude(
            status__in=[ArtistAuth.STATUS_ACCEPTED, ArtistAuth.STATUS_REJECTED]
        ).order_by('-created_at')
        
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(pending_auths, request)
        serializer = AdminArtistAuthSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

class AdminPendingArtistDetailView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request, pk):
        auth = get_object_or_404(ArtistAuth, pk=pk)
        serializer = AdminArtistAuthSerializer(auth)
        return Response(serializer.data)

    def put(self, request, pk):
        auth = get_object_or_404(ArtistAuth, pk=pk)
        serializer = AdminArtistAuthSerializer(auth, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request, pk):
        auth = get_object_or_404(ArtistAuth, pk=pk)
        serializer = AdminArtistAuthSerializer(auth, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        auth = get_object_or_404(ArtistAuth, pk=pk)
        auth.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminHomeSummaryView(APIView):
    """Return overall stream + pay summary for the admin home/dashboard.

    Response includes counts and pay sums for all-time, last 30 days, last 7 days, and last 24 hours.
    {
      "total": int,
      "last_30_days": int,
      "last_7_days": int,
      "last_24_hours": int,
      "total_pay": float,
      "pay_last_30_days": float,
      "pay_last_7_days": float,
      "pay_last_24_hours": float,
    }
    """
    permission_classes = [permissions.IsAdminUser]

    def _sum_pay(self, qs):
        val = qs.aggregate(total=Sum('pay'))['total']
        if val is None:
            return 0.0
        if isinstance(val, Decimal):
            return float(val)
        return float(val)

    def get(self, request):
        now = timezone.now()
        last_24 = now - timedelta(days=1)
        last_7 = now - timedelta(days=7)
        last_30 = now - timedelta(days=30)

        total = PlayCount.objects.count()
        last_30_count = PlayCount.objects.filter(created_at__gte=last_30).count()
        last_7_count = PlayCount.objects.filter(created_at__gte=last_7).count()
        last_24_count = PlayCount.objects.filter(created_at__gte=last_24).count()

        total_pay = self._sum_pay(PlayCount.objects.all())
        pay_last_30 = self._sum_pay(PlayCount.objects.filter(created_at__gte=last_30))
        pay_last_7 = self._sum_pay(PlayCount.objects.filter(created_at__gte=last_7))
        pay_last_24 = self._sum_pay(PlayCount.objects.filter(created_at__gte=last_24))

        # Audience users: users who have the audience role
        try:
            audience_count = User.objects.filter(roles__contains=User.ROLE_AUDIENCE).count()
        except Exception:
            # Fallback for databases/ORM that don't support JSON contains lookups
            audience_count = User.objects.filter(roles__icontains=User.ROLE_AUDIENCE).count()

        # Artist profiles count
        artist_profiles_count = Artist.objects.count()

        return Response({
            'total': total,
            'last_30_days': last_30_count,
            'last_7_days': last_7_count,
            'last_24_hours': last_24_count,
            'total_pay': total_pay,
            'pay_last_30_days': pay_last_30,
            'pay_last_7_days': pay_last_7,
            'pay_last_24_hours': pay_last_24,
            'audience_count': audience_count,
            'artist_profiles_count': artist_profiles_count,
        })


class AdminUserSearchView(APIView):
    """Search/list users, artists or pending artist submissions for admin.

    Query params:
    - type: one of ['audience', 'artist', 'pend_artist'] (default 'audience')

    Results are paginated using `AdminPagination`.
    """
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        typ = request.query_params.get('type', 'audience')
        paginator = AdminPagination()

        if typ == 'audience':
            qs = User.objects.filter(roles__contains=User.ROLE_AUDIENCE).order_by('-date_joined')
            serializer_cls = AdminUserSerializer
        elif typ == 'artist':
            qs = Artist.objects.all().order_by('-created_at')
            serializer_cls = AdminArtistSerializer
        elif typ == 'pend_artist':
            qs = ArtistAuth.objects.exclude(status__in=[ArtistAuth.STATUS_ACCEPTED, ArtistAuth.STATUS_REJECTED]).order_by('-created_at')
            serializer_cls = AdminArtistAuthSerializer
        else:
            return Response({'error': 'Invalid type parameter. Use audience|artist|pend_artist'}, status=status.HTTP_400_BAD_REQUEST)

        result_page = paginator.paginate_queryset(qs, request)
        serializer = serializer_cls(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)


class AdminSongListView(APIView):
    """List songs for admin with status filtering.
    
    Query params:
    - status: filter by song status (default 'published')
    """
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        status_filter = request.query_params.get('status', Song.STATUS_PUBLISHED)
        songs = Song.objects.filter(status=status_filter).order_by('-created_at')
        
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(songs, request)
        serializer = AdminSongSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)


class AdminSongDetailView(APIView):
    """Retrieve, update or delete a song for admin.
    
    Supports flat form-data for file uploads.
    """
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request, pk):
        song = get_object_or_404(Song, pk=pk)
        serializer = AdminSongSerializer(song)
        return Response(serializer.data)

    def patch(self, request, pk):
        song = get_object_or_404(Song, pk=pk)
        return self._update_song(request, song, partial=True)

    def put(self, request, pk):
        song = get_object_or_404(Song, pk=pk)
        return self._update_song(request, song, partial=False)

    def delete(self, request, pk):
        song = get_object_or_404(Song, pk=pk)
        song.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _update_song(self, request, song, partial=False):
        data = request.data.copy()
        
        # Ensure list fields are correctly extracted from QueryDict
        for field in ['featured_artists', 'producers', 'composers', 'lyricists', 'genres', 'sub_genres', 'moods', 'tags']:
            if field in data and hasattr(data, 'getlist'):
                # Only use getlist if it's actually a list of values
                # Sometimes frontend might send a single value or a comma-separated string
                val = data.getlist(field)
                if len(val) == 1 and ',' in val[0]:
                    data[field] = [v.strip() for v in val[0].split(',')]
                else:
                    data[field] = val

        # Handle audio file upload
        audio_file = request.FILES.get('audio_file_upload')
        if audio_file:
            title = data.get('title', song.title)
            artist = song.artist
            # If artist is being changed in the same request
            if 'artist' in data:
                try:
                    artist = Artist.objects.get(pk=data['artist'])
                except Artist.DoesNotExist:
                    pass
            
            artist_name = artist.artistic_name or artist.name
            
            duration, bitrate, format_ext = get_audio_info(audio_file)
            if not format_ext:
                _, ext = os.path.splitext(audio_file.name)
                format_ext = ext.lstrip('.').lower()
            
            bitrate_str = str(bitrate) if bitrate else "wav"
            version = Song.objects.filter(artist=artist, title=title).count() + 1
            
            safe_title = "".join([c for c in title if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            safe_artist = "".join([c for c in artist_name if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            filename = f"{safe_artist} - {safe_title} ({bitrate_str}){version}.{format_ext}"
            
            audio_url, _ = upload_file_to_r2(audio_file, folder='songs', custom_filename=filename)
            data['audio_file'] = audio_url
            data['duration_seconds'] = duration
            data['original_format'] = format_ext
            
            # Handle 128kbps conversion
            if format_ext == 'mp3' and bitrate and bitrate > 128:
                try:
                    converted_file = convert_to_128kbps(audio_file)
                    conv_filename = f"{safe_artist} - {safe_title} (128){version}.mp3"
                    converted_url, _ = upload_file_to_r2(converted_file, folder='songs', custom_filename=conv_filename)
                    data['converted_audio_url'] = converted_url
                except Exception as e:
                    print(f"Admin conversion failed: {e}")

        # Handle cover image upload
        cover_image = request.FILES.get('cover_image_upload')
        if cover_image:
            title = data.get('title', song.title)
            artist = song.artist
            if 'artist' in data:
                try:
                    artist = Artist.objects.get(pk=data['artist'])
                except Artist.DoesNotExist:
                    pass
            artist_name = artist.artistic_name or artist.name
            version = Song.objects.filter(artist=artist, title=title).count() + 1
            
            safe_title = "".join([c for c in title if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            safe_artist = "".join([c for c in artist_name if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            
            cover_filename = f"{safe_artist} - {safe_title} {version}_cover"
            cover_url, _ = upload_file_to_r2(cover_image, folder='covers', custom_filename=cover_filename)
            data['cover_image'] = cover_url

        serializer = AdminSongSerializer(song, data=data, partial=partial)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class AdminReportListView(APIView):
    """List reports for admin with filtering.
    
    Query params:
    - has_reviewed: filter by review status (true/false)
    - type: filter by target type (song/artist)
    """
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        qs = Report.objects.all().order_by('-created_at')
        
        has_reviewed = request.query_params.get('has_reviewed')
        if has_reviewed is not None:
            qs = qs.filter(has_reviewed=has_reviewed.lower() == 'true')
            
        typ = request.query_params.get('type')
        if typ == 'song':
            qs = qs.filter(song__isnull=False)
        elif typ == 'artist':
            qs = qs.filter(artist__isnull=False)
            
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(qs, request)
        serializer = AdminReportSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)


class AdminReportDetailView(APIView):
    """Retrieve or update a report for admin."""
    permission_classes = [permissions.IsAdminUser]

    def get(self, request, pk):
        report = get_object_or_404(Report, pk=pk)
        serializer = AdminReportSerializer(report)
        return Response(serializer.data)

    def put(self, request, pk):
        report = get_object_or_404(Report, pk=pk)
        data = request.data.copy()
        
        # If has_reviewed is being set to true, set reviewed_at
        if data.get('has_reviewed') is True or data.get('has_reviewed') == 'true':
            if not report.has_reviewed:
                data['reviewed_at'] = timezone.now()
        
        serializer = AdminReportSerializer(report, data=data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class AdminPlayConfigurationView(APIView):
    """View for admin to manage global play and price settings."""
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        config = PlayConfiguration.objects.last()
        if not config:
            config = PlayConfiguration.objects.create()
        serializer = AdminPlayConfigurationSerializer(config)
        return Response(serializer.data)

    def post(self, request):
        config = PlayConfiguration.objects.last()
        if not config:
            config = PlayConfiguration.objects.create()
        
        serializer = AdminPlayConfigurationSerializer(config, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class AdminBannerAdListView(APIView):
    """List and create banner ads for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request):
        ads = BannerAd.objects.all().order_by('-created_at')
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(ads, request)
        serializer = AdminBannerAdSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        data = request.data.copy()
        image_file = request.FILES.get('image_upload')
        if image_file:
            safe_title = "".join([c for c in data.get('title', 'banner') if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            filename = f"banner_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}"
            image_url, _ = upload_file_to_r2(image_file, folder='ads/banners', custom_filename=filename)
            data['image'] = image_url

        serializer = AdminBannerAdSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class AdminBannerAdDetailView(APIView):
    """Retrieve, update or delete a banner ad for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request, pk):
        ad = get_object_or_404(BannerAd, pk=pk)
        serializer = AdminBannerAdSerializer(ad)
        return Response(serializer.data)

    def patch(self, request, pk):
        ad = get_object_or_404(BannerAd, pk=pk)
        data = request.data.copy()
        image_file = request.FILES.get('image_upload')
        if image_file:
            safe_title = "".join([c for c in data.get('title', ad.title) if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            filename = f"banner_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}"
            image_url, _ = upload_file_to_r2(image_file, folder='ads/banners', custom_filename=filename)
            data['image'] = image_url

        serializer = AdminBannerAdSerializer(ad, data=data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        ad = get_object_or_404(BannerAd, pk=pk)
        ad.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminAudioAdListView(APIView):
    """List and create audio ads for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request):
        ads = AudioAd.objects.all().order_by('-created_at')
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(ads, request)
        serializer = AdminAudioAdSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        data = request.data.copy()
        audio_file = request.FILES.get('audio_upload')
        if audio_file:
            safe_title = "".join([c for c in data.get('title', 'audio_ad') if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            filename = f"audio_ad_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}"
            audio_url, _ = upload_file_to_r2(audio_file, folder='ads/audio', custom_filename=filename)
            data['audio_url'] = audio_url
            
            # Try to get duration if not provided
            if not data.get('duration'):
                duration, _, _ = get_audio_info(audio_file)
                if duration:
                    data['duration'] = duration

        image_file = request.FILES.get('image_cover_upload')
        if image_file:
            safe_title = "".join([c for c in data.get('title', 'audio_ad') if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            filename = f"audio_ad_cover_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}"
            image_url, _ = upload_file_to_r2(image_file, folder='ads/audio/covers', custom_filename=filename)
            data['image_cover'] = image_url

        serializer = AdminAudioAdSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class AdminAudioAdDetailView(APIView):
    """Retrieve, update or delete an audio ad for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request, pk):
        ad = get_object_or_404(AudioAd, pk=pk)
        serializer = AdminAudioAdSerializer(ad)
        return Response(serializer.data)

    def patch(self, request, pk):
        ad = get_object_or_404(AudioAd, pk=pk)
        data = request.data.copy()
        
        audio_file = request.FILES.get('audio_upload')
        if audio_file:
            safe_title = "".join([c for c in data.get('title', ad.title) if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            filename = f"audio_ad_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}"
            audio_url, _ = upload_file_to_r2(audio_file, folder='ads/audio', custom_filename=filename)
            data['audio_url'] = audio_url
            
            if not data.get('duration'):
                duration, _, _ = get_audio_info(audio_file)
                if duration:
                    data['duration'] = duration

        image_file = request.FILES.get('image_cover_upload')
        if image_file:
            safe_title = "".join([c for c in data.get('title', ad.title) if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            filename = f"audio_ad_cover_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}"
            image_url, _ = upload_file_to_r2(image_file, folder='ads/audio/covers', custom_filename=filename)
            data['image_cover'] = image_url

        serializer = AdminAudioAdSerializer(ad, data=data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        ad = get_object_or_404(AudioAd, pk=pk)
        ad.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminAlbumListView(APIView):
    """List albums for admin.
    
    Filters out "singles" (albums with only 1 song where that song is marked as is_single).
    """
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        # Filter out albums that are effectively singles
        # We'll filter albums that have more than 1 song OR have 1 song that is NOT a single.
        qs = Album.objects.annotate(song_count=Count('songs')).filter(song_count__gt=0)
        
        # Exclude albums where song_count == 1 AND the song is_single=True
        qs = qs.exclude(song_count=1, songs__is_single=True)
        
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(qs, request)
        serializer = AdminAlbumSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)


class AdminAlbumDetailView(APIView):
    """Retrieve, update or delete an album for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request, pk):
        album = get_object_or_404(Album, pk=pk)
        serializer = AdminAlbumSerializer(album)
        return Response(serializer.data)

    def patch(self, request, pk):
        album = get_object_or_404(Album, pk=pk)
        return self._update_album(request, album, partial=True)

    def put(self, request, pk):
        album = get_object_or_404(Album, pk=pk)
        return self._update_album(request, album, partial=False)

    def delete(self, request, pk):
        album = get_object_or_404(Album, pk=pk)
        album.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _update_album(self, request, album, partial=False):
        data = request.data.copy()
        
        # Handle cover image upload
        cover_image = request.FILES.get('cover_image_upload')
        if cover_image:
            artist = album.artist
            if 'artist' in data:
                try:
                    artist = Artist.objects.get(pk=data['artist'])
                except Artist.DoesNotExist:
                    pass
            artist_name = artist.artistic_name or artist.name
            safe_title = "".join([c for c in data.get('title', album.title) if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            safe_artist = "".join([c for c in artist_name if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            cover_filename = f"{safe_artist} - {safe_title}_album_cover"
            cover_url, _ = upload_file_to_r2(cover_image, folder='covers', custom_filename=cover_filename)
            data['cover_image'] = cover_url

        serializer = AdminAlbumSerializer(album, data=data, partial=partial)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class AdminAlbumSongActionView(APIView):
    """Actions on songs within an album: remove from album or delete song."""
    permission_classes = [permissions.IsAdminUser]

    def post(self, request, album_id, song_id):
        action = request.data.get('action') # 'remove' or 'delete'
        album = get_object_or_404(Album, pk=album_id)
        song = get_object_or_404(Song, pk=song_id, album=album)
        
        if action == 'remove':
            song.album = None
            song.save()
            return Response({"message": "Song removed from album"})
        elif action == 'delete':
            song.delete()
            return Response({"message": "Song deleted successfully"})
        else:
            return Response({"error": "Invalid action. Use 'remove' or 'delete'"}, status=status.HTTP_400_BAD_REQUEST)


class AdminFinanceSummaryView(APIView):
    """Summary of payments and deposit requests."""
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        last_7_days = now - timedelta(days=7)
        last_30_days = now - timedelta(days=30)

        # Custom period
        start_param = request.query_params.get('start')
        end_param = request.query_params.get('end')
        
        def get_stats(start_date, end_date=None):
            q_pay = Q(created_at__gte=start_date, status=PaymentTransaction.STATUS_SUCCESS)
            q_dep = Q(submission_date__gte=start_date, status__in=[DepositRequest.STATUS_APPROVED, DepositRequest.STATUS_DONE])
            
            if end_date:
                q_pay &= Q(created_at__lte=end_date)
                q_dep &= Q(submission_date__lte=end_date)
            
            total_payments = PaymentTransaction.objects.filter(q_pay).aggregate(total=Sum('amount'))['total'] or 0
            total_deposits = DepositRequest.objects.filter(q_dep).aggregate(total=Sum('amount'))['total'] or 0
            
            return {
                'total_payments': total_payments,
                'total_deposits': total_deposits,
                'count_payments': PaymentTransaction.objects.filter(q_pay).count(),
                'count_deposits': DepositRequest.objects.filter(q_dep).count(),
            }

        summary = {
            'today': get_stats(today_start),
            'last_7_days': get_stats(last_7_days),
            'last_30_days': get_stats(last_30_days),
            'all_time': get_stats(timezone.make_aware(timezone.datetime(2000, 1, 1))),
        }

        if start_param and end_param:
            try:
                # Handle both YYYY-MM-DD and ISO format
                if len(start_param) == 10:
                    start_dt = timezone.make_aware(timezone.datetime.strptime(start_param, '%Y-%m-%d'))
                else:
                    start_dt = timezone.make_aware(timezone.datetime.fromisoformat(start_param))
                
                if len(end_param) == 10:
                    end_dt = timezone.make_aware(timezone.datetime.strptime(end_param, '%Y-%m-%d'))
                    end_dt = end_dt.replace(hour=23, minute=59, second=59)
                else:
                    end_dt = timezone.make_aware(timezone.datetime.fromisoformat(end_param))
                    
                summary['custom_period'] = get_stats(start_dt, end_dt)
            except (ValueError, TypeError):
                summary['custom_period_error'] = "Invalid date format. Use YYYY-MM-DD or ISO format."

        return Response(summary)


class AdminPaymentTransactionListView(APIView):
    """List payment transactions with filtering."""
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        queryset = PaymentTransaction.objects.all().order_by('-created_at')
        
        status_filter = request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
            
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(queryset, request)
        serializer = AdminPaymentTransactionSerializer(result_page, many=True)
        
        response = paginator.get_paginated_response(serializer.data)
        # Add extra info to the response data
        response.data['total_amount'] = queryset.aggregate(total=Sum('amount'))['total'] or 0
        response.data['total_count'] = queryset.count()
        return response


class AdminDepositRequestListView(APIView):
    """List deposit requests with filtering."""
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        queryset = DepositRequest.objects.all().order_by('-submission_date')
        
        status_filter = request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
            
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(queryset, request)
        serializer = AdminDepositRequestSerializer(result_page, many=True)
        
        response = paginator.get_paginated_response(serializer.data)
        # Add extra info to the response data
        response.data['total_amount'] = queryset.aggregate(total=Sum('amount'))['total'] or 0
        response.data['total_count'] = queryset.count()
        return response


class AdminSearchSectionListView(APIView):
    """List and create search sections for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request):
        sections = SearchSection.objects.all().order_by('-created_at')
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(sections, request)
        serializer = AdminSearchSectionSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        data = request.data.copy()
        icon_file = request.FILES.get('icon_logo_upload')
        if icon_file:
            safe_title = "".join([c for c in data.get('title', 'section') if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            filename = f"section_icon_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}"
            icon_url, _ = upload_file_to_r2(icon_file, folder='sections/icons', custom_filename=filename)
            data['icon_logo'] = icon_url

        serializer = AdminSearchSectionSerializer(data=data)
        if serializer.is_valid():
            serializer.save(created_by=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class AdminSearchSectionDetailView(APIView):
    """Retrieve, update or delete a search section for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request, pk):
        section = get_object_or_404(SearchSection, pk=pk)
        serializer = AdminSearchSectionSerializer(section)
        return Response(serializer.data)

    def patch(self, request, pk):
        section = get_object_or_404(SearchSection, pk=pk)
        data = request.data.copy()
        icon_file = request.FILES.get('icon_logo_upload')
        if icon_file:
            safe_title = "".join([c for c in data.get('title', section.title) if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            filename = f"section_icon_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}"
            icon_url, _ = upload_file_to_r2(icon_file, folder='sections/icons', custom_filename=filename)
            data['icon_logo'] = icon_url

        serializer = AdminSearchSectionSerializer(section, data=data, partial=True)
        if serializer.is_valid():
            serializer.save(updated_by=request.user)
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        section = get_object_or_404(SearchSection, pk=pk)
        section.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminEventPlaylistListView(APIView):
    """List and create event playlist groups for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request):
        events = EventPlaylist.objects.all().order_by('-created_at')
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(events, request)
        serializer = AdminEventPlaylistSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        data = request.data.copy()
        cover_file = request.FILES.get('cover_image_upload')
        if cover_file:
            safe_title = "".join([c for c in data.get('title', 'event') if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            filename = f"event_cover_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}"
            cover_url, _ = upload_file_to_r2(cover_file, folder='events/covers', custom_filename=filename)
            data['cover_image'] = cover_url

        serializer = AdminEventPlaylistSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class AdminEventPlaylistDetailView(APIView):
    """Retrieve, update or delete an event playlist group for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request, pk):
        event = get_object_or_404(EventPlaylist, pk=pk)
        serializer = AdminEventPlaylistSerializer(event)
        return Response(serializer.data)

    def patch(self, request, pk):
        event = get_object_or_404(EventPlaylist, pk=pk)
        data = request.data.copy()
        cover_file = request.FILES.get('cover_image_upload')
        if cover_file:
            safe_title = "".join([c for c in data.get('title', event.title) if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            filename = f"event_cover_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}"
            cover_url, _ = upload_file_to_r2(cover_file, folder='events/covers', custom_filename=filename)
            data['cover_image'] = cover_url

        serializer = AdminEventPlaylistSerializer(event, data=data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        event = get_object_or_404(EventPlaylist, pk=pk)
        event.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminPlaylistListView(APIView):
    """List and create playlists for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request):
        playlists = Playlist.objects.all().order_by('-created_at')
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(playlists, request)
        serializer = AdminPlaylistSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        data = request.data.copy()
        cover_file = request.FILES.get('cover_image_upload')
        if cover_file:
            safe_title = "".join([c for c in data.get('title', 'playlist') if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            filename = f"playlist_cover_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}"
            cover_url, _ = upload_file_to_r2(cover_file, folder='playlists/covers', custom_filename=filename)
            data['cover_image'] = cover_url

        serializer = AdminPlaylistSerializer(data=data)
        if serializer.is_valid():
            serializer.save(created_by=Playlist.CREATED_BY_ADMIN)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class AdminPlaylistDetailView(APIView):
    """Retrieve, update or delete a playlist for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request, pk):
        playlist = get_object_or_404(Playlist, pk=pk)
        serializer = AdminPlaylistSerializer(playlist)
        return Response(serializer.data)

    def patch(self, request, pk):
        playlist = get_object_or_404(Playlist, pk=pk)
        data = request.data.copy()
        cover_file = request.FILES.get('cover_image_upload')
        if cover_file:
            safe_title = "".join([c for c in data.get('title', playlist.title) if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            filename = f"playlist_cover_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}"
            cover_url, _ = upload_file_to_r2(cover_file, folder='playlists/covers', custom_filename=filename)
            data['cover_image'] = cover_url

        serializer = AdminPlaylistSerializer(playlist, data=data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        playlist = get_object_or_404(Playlist, pk=pk)
        playlist.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
