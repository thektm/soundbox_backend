from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions, serializers
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes, inline_serializer
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
    AdminSearchSectionSerializer, AdminEventPlaylistSerializer, AdminPlaylistSerializer,
    AdminEmployeeSerializer
)
from rest_framework.parsers import MultiPartParser, FormParser
from .utils import upload_file_to_r2, convert_to_128kbps, get_audio_info, make_safe_filename, generate_signed_r2_url
import os

class AdminPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100

@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminUserListView(APIView):
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="لیست کاربران",
        description="دریافت لیست تمامی کاربران (شنوندگان) با قابلیت صفحه‌بندی. به صورت پیش‌فرض بر اساس تاریخ عضویت مرتب شده‌اند.",
        responses={200: AdminUserSerializer(many=True)}
    )
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

@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminUserDetailView(APIView):
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="جزئیات کاربر",
        description="دریافت اطلاعات کامل یک کاربر خاص بر اساس شناسه.",
        responses={200: AdminUserSerializer}
    )
    def get(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        serializer = AdminUserSerializer(user)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش کامل کاربر",
        description="ویرایش تمامی فیلدهای یک کاربر.",
        request=AdminUserSerializer,
        responses={200: AdminUserSerializer}
    )
    def put(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        serializer = AdminUserSerializer(user, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="ویرایش جزئی کاربر",
        description="ویرایش برخی از فیلدهای یک کاربر.",
        request=AdminUserSerializer,
        responses={200: AdminUserSerializer}
    )
    def patch(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        serializer = AdminUserSerializer(user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف کاربر",
        description="حذف کامل یک کاربر از سیستم.",
        responses={204: None}
    )
    def delete(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminUserBanView(APIView):
    """Ban a user and delete their artist profile and content."""
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="مسدود کردن کاربر",
        description="مسدود کردن کاربر و حذف پروفایل هنرمند و تمامی محتواهای مرتبط (آهنگ‌ها و آلبوم‌ها).",
        request=inline_serializer(
            name='AdminUserBanRequest',
            fields={'user_id': serializers.IntegerField()}
        ),
        responses={
            200: inline_serializer(
                name='AdminUserBanResponse',
                fields={'message': serializers.CharField()}
            )
        }
    )
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

@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminArtistListView(APIView):
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="لیست هنرمندان",
        description="دریافت لیست تمامی هنرمندان تایید شده در سیستم.",
        responses={200: AdminArtistSerializer(many=True)}
    )
    def get(self, request):
        artists = Artist.objects.all().order_by('-created_at')
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(artists, request)
        serializer = AdminArtistSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminArtistDetailView(APIView):
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="جزئیات هنرمند",
        description="دریافت اطلاعات کامل یک هنرمند خاص.",
        responses={200: AdminArtistSerializer}
    )
    def get(self, request, pk):
        artist = get_object_or_404(Artist, pk=pk)
        serializer = AdminArtistSerializer(artist)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش کامل هنرمند",
        description="ویرایش تمامی اطلاعات یک هنرمند.",
        request=AdminArtistSerializer,
        responses={200: AdminArtistSerializer}
    )
    def put(self, request, pk):
        artist = get_object_or_404(Artist, pk=pk)
        serializer = AdminArtistSerializer(artist, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="ویرایش جزئی هنرمند",
        description="ویرایش برخی از اطلاعات یک هنرمند.",
        request=AdminArtistSerializer,
        responses={200: AdminArtistSerializer}
    )
    def patch(self, request, pk):
        artist = get_object_or_404(Artist, pk=pk)
        serializer = AdminArtistSerializer(artist, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف هنرمند",
        description="حذف پروفایل هنرمند از سیستم.",
        responses={204: None}
    )
    def delete(self, request, pk):
        artist = get_object_or_404(Artist, pk=pk)
        artist.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminPendingArtistListView(APIView):
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="لیست درخواست‌های هنرمند",
        description="دریافت لیست درخواست‌های عضویت هنرمندان که هنوز تایید یا رد نشده‌اند.",
        responses={200: AdminArtistAuthSerializer(many=True)}
    )
    def get(self, request):
        # records of artistAuth with not accepted or rejected status
        pending_auths = ArtistAuth.objects.exclude(
            status__in=[ArtistAuth.STATUS_ACCEPTED, ArtistAuth.STATUS_REJECTED]
        ).order_by('-created_at')
        
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(pending_auths, request)
        serializer = AdminArtistAuthSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminPendingArtistDetailView(APIView):
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="جزئیات درخواست هنرمند",
        description="دریافت جزئیات یک درخواست خاص برای بررسی.",
        responses={200: AdminArtistAuthSerializer}
    )
    def get(self, request, pk):
        auth = get_object_or_404(ArtistAuth, pk=pk)
        serializer = AdminArtistAuthSerializer(auth)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش کامل درخواست",
        description="ویرایش تمامی اطلاعات یک درخواست عضویت.",
        request=AdminArtistAuthSerializer,
        responses={200: AdminArtistAuthSerializer}
    )
    def put(self, request, pk):
        auth = get_object_or_404(ArtistAuth, pk=pk)
        serializer = AdminArtistAuthSerializer(auth, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="تایید یا رد درخواست هنرمند",
        description="تغییر وضعیت درخواست هنرمند (تایید، رد یا در حال بررسی).",
        request=AdminArtistAuthSerializer,
        responses={200: AdminArtistAuthSerializer}
    )
    def patch(self, request, pk):
        auth = get_object_or_404(ArtistAuth, pk=pk)
        serializer = AdminArtistAuthSerializer(auth, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف درخواست هنرمند",
        description="حذف یک درخواست عضویت از سیستم.",
        responses={204: None}
    )
    def delete(self, request, pk):
        auth = get_object_or_404(ArtistAuth, pk=pk)
        auth.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminHomeSummaryView(APIView):
    """Return overall stream + pay summary for the admin home/dashboard."""
    permission_classes = [permissions.IsAdminUser]

    def _sum_pay(self, qs):
        val = qs.aggregate(total=Sum('pay'))['total']
        if val is None:
            return 0.0
        if isinstance(val, Decimal):
            return float(val)
        return float(val)

    @extend_schema(
        summary="خلاصه وضعیت داشبورد ادمین",
        description="دریافت آمار کلی پخش‌ها، پرداخت‌ها، تعداد کاربران و هنرمندان برای داشبورد مدیریت.",
        responses={
            200: inline_serializer(
                name='AdminDashboardResponse',
                fields={
                    'total': serializers.IntegerField(),
                    'last_30_days': serializers.IntegerField(),
                    'last_7_days': serializers.IntegerField(),
                    'last_24_hours': serializers.IntegerField(),
                    'total_pay': serializers.FloatField(),
                    'pay_last_30_days': serializers.FloatField(),
                    'pay_last_7_days': serializers.FloatField(),
                    'pay_last_24_hours': serializers.FloatField(),
                    'audience_count': serializers.IntegerField(),
                    'artist_profiles_count': serializers.IntegerField(),
                }
            )
        }
    )
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


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminUserSearchView(APIView):
    """Search/list users, artists or pending artist submissions for admin."""
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="جستجوی کاربران و هنرمندان",
        description="جستجو و لیست کردن کاربران، هنرمندان یا درخواست‌های در انتظار تایید بر اساس پارامتر type.",
        parameters=[
            OpenApiParameter("type", OpenApiTypes.STR, description="نوع جستجو: audience, artist, pend_artist", default="audience")
        ],
        responses={200: AdminUserSerializer(many=True)}
    )
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


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminSongListView(APIView):
    """List songs for admin with status filtering."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="لیست آهنگ‌ها",
        description="دریافت لیست تمامی آهنگ‌ها با قابلیت فیلتر بر اساس وضعیت (منتشر شده، در انتظار و غیره).",
        parameters=[
            OpenApiParameter("status", OpenApiTypes.STR, description="وضعیت آهنگ (مثلا published)", default="published")
        ],
        responses={200: AdminSongSerializer(many=True)}
    )
    def get(self, request):
        status_filter = request.query_params.get('status', Song.STATUS_PUBLISHED)
        songs = Song.objects.filter(status=status_filter).order_by('-created_at')
        
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(songs, request)
        serializer = AdminSongSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @extend_schema(
        summary="آپلود آهنگ جدید توسط ادمین",
        description="آپلود فایل صوتی آهنگ به همراه متادیتا و تصویر کاور توسط ادمین برای هنرمند مشخص.",
        request=AdminSongSerializer,
        responses={201: AdminSongSerializer}
    )
    def post(self, request):
        serializer = AdminSongSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        data = serializer.validated_data
        
        try:
            # Get artist
            artist = data['artist']
            
            # Build filename: "Artist - Title (feat. X)" or "Artist - Title"
            title = data['title']
            featured = data.get('featured_artists', [])
            artist_name = artist.artistic_name or artist.name
            if featured:
                filename_base = f"{artist_name} - {title} (feat. {', '.join(featured)})"
            else:
                filename_base = f"{artist_name} - {title}"
            
            safe_filename_base = make_safe_filename(filename_base)
            
            # Handle audio file upload
            audio_url = ""
            converted_audio_url = None
            duration = None
            original_format = None
            if 'audio_file_upload' in request.FILES:
                audio_file = request.FILES['audio_file_upload']
                audio_filename = f"{safe_filename_base}.{audio_file.name.split('.')[-1]}"
                audio_url, original_format = upload_file_to_r2(
                    audio_file,
                    folder='songs',
                    custom_filename=audio_filename
                )
                
                # Get audio info
                duration, bitrate, original_format = get_audio_info(audio_file)
                if not original_format:
                    original_format = audio_file.name.split('.')[-1].lower()
                
                # Convert to 128kbps and upload
                if original_format != 'mp3' or bitrate is None or bitrate > 128:
                    try:
                        # Reset file pointer before conversion
                        if hasattr(audio_file, 'seek'):
                            audio_file.seek(0)
                        
                        converted_file = convert_to_128kbps(audio_file)
                        converted_filename = f"{safe_filename_base}_128.mp3"
                        converted_audio_url, _ = upload_file_to_r2(
                            converted_file,
                            folder='songs/128',
                            custom_filename=converted_filename
                        )
                    except Exception as e:
                        # Log error but don't fail the whole upload
                        print(f"Conversion failed: {e}")
            
            # Handle cover image upload
            cover_url = ""
            if 'cover_image_upload' in request.FILES:
                cover_file = request.FILES['cover_image_upload']
                cover_filename = f"{safe_filename_base}_cover.{cover_file.name.split('.')[-1]}"
                cover_url, _ = upload_file_to_r2(
                    cover_file,
                    folder='covers',
                    custom_filename=cover_filename
                )
            
            # Prepare song data
            song_data = dict(data)
            song_data['audio_file'] = audio_url
            song_data['converted_audio_url'] = converted_audio_url
            song_data['cover_image'] = cover_url
            song_data['original_format'] = original_format
            song_data['duration_seconds'] = duration
            song_data['uploader'] = request.user
            
            # Remove file fields and many-to-many from data for create
            song_data.pop('audio_file_upload', None)
            song_data.pop('cover_image_upload', None)
            genres = song_data.pop('genres', [])
            sub_genres = song_data.pop('sub_genres', [])
            moods = song_data.pop('moods', [])
            tags = song_data.pop('tags', [])
            
            song = Song.objects.create(**song_data)
            
            # Add many-to-many relationships
            song.genres.set(genres)
            song.sub_genres.set(sub_genres)
            song.moods.set(moods)
            song.tags.set(tags)
            
            return Response(
                AdminSongSerializer(song).data,
                status=status.HTTP_201_CREATED
            )
            
        except Artist.DoesNotExist:
            return Response(
                {'error': 'Artist not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminSongDetailView(APIView):
    """Retrieve, update or delete a song for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="جزئیات آهنگ",
        description="دریافت اطلاعات کامل یک آهنگ خاص.",
        responses={200: AdminSongSerializer}
    )
    def get(self, request, pk):
        song = get_object_or_404(Song, pk=pk)
        serializer = AdminSongSerializer(song)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش جزئی آهنگ",
        description="ویرایش برخی از فیلدهای آهنگ و آپلود فایل صوتی یا کاور جدید.",
        request=AdminSongSerializer,
        responses={200: AdminSongSerializer}
    )
    def patch(self, request, pk):
        song = get_object_or_404(Song, pk=pk)
        return self._update_song(request, song, partial=True)

    @extend_schema(
        summary="ویرایش کامل آهنگ",
        description="ویرایش تمامی فیلدهای آهنگ.",
        request=AdminSongSerializer,
        responses={200: AdminSongSerializer}
    )
    def put(self, request, pk):
        song = get_object_or_404(Song, pk=pk)
        return self._update_song(request, song, partial=False)

    @extend_schema(
        summary="حذف آهنگ",
        description="حذف کامل یک آهنگ از سیستم.",
        responses={204: None}
    )
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
            
            # Build filename base
            featured = data.get('featured_artists', song.featured_artists)
            if featured:
                filename_base = f"{artist_name} - {title} (feat. {', '.join(featured)})"
            else:
                filename_base = f"{artist_name} - {title}"
            
            safe_filename_base = filename_base
            audio_filename = f"{safe_filename_base}.{format_ext}"
            
            audio_url, _ = upload_file_to_r2(audio_file, folder='songs', custom_filename=audio_filename)
            data['audio_file'] = audio_url
            data['duration_seconds'] = duration
            data['original_format'] = format_ext
            
            # Handle 128kbps conversion
            if format_ext != 'mp3' or bitrate is None or bitrate > 128:
                try:
                    if hasattr(audio_file, 'seek'):
                        audio_file.seek(0)
                    converted_file = convert_to_128kbps(audio_file)
                    conv_filename = f"{safe_filename_base}_128.mp3"
                    converted_url, _ = upload_file_to_r2(converted_file, folder='songs/128', custom_filename=conv_filename)
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
            
            featured = data.get('featured_artists', song.featured_artists)
            if featured:
                filename_base = f"{artist_name} - {title} (feat. {', '.join(featured)})"
            else:
                filename_base = f"{artist_name} - {title}"
            
            safe_filename_base = filename_base
            _, ext = os.path.splitext(cover_image.name)
            cover_filename = f"{safe_filename_base}_cover{ext}"
            cover_url, _ = upload_file_to_r2(cover_image, folder='covers', custom_filename=cover_filename)
            data['cover_image'] = cover_url

        serializer = AdminSongSerializer(song, data=data, partial=partial)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminReportListView(APIView):
    """List reports for admin with filtering."""
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="لیست گزارش‌ها",
        description="دریافت لیست گزارش‌های تخلف ثبت شده توسط کاربران با قابلیت فیلتر بر اساس وضعیت بررسی و نوع هدف (آهنگ یا هنرمند).",
        parameters=[
            OpenApiParameter("has_reviewed", OpenApiTypes.BOOL, description="فیلتر بر اساس وضعیت بررسی شده"),
            OpenApiParameter("type", OpenApiTypes.STR, description="فیلتر بر اساس نوع: song یا artist")
        ],
        responses={200: AdminReportSerializer(many=True)}
    )
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


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminReportDetailView(APIView):
    """Retrieve or update a report for admin."""
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="جزئیات گزارش",
        description="دریافت اطلاعات کامل یک گزارش خاص.",
        responses={200: AdminReportSerializer}
    )
    def get(self, request, pk):
        report = get_object_or_404(Report, pk=pk)
        serializer = AdminReportSerializer(report)
        return Response(serializer.data)

    @extend_schema(
        summary="بروزرسانی گزارش",
        description="تغییر وضعیت بررسی گزارش.",
        request=AdminReportSerializer,
        responses={200: AdminReportSerializer}
    )
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

    @extend_schema(
        summary="حذف گزارش",
        description="حذف یک گزارش از سیستم.",
        responses={204: None}
    )
    def delete(self, request, pk):
        report = get_object_or_404(Report, pk=pk)
        report.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminPlayConfigurationView(APIView):
    """View for admin to manage global play and price settings."""
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="تنظیمات پخش و قیمت‌گذاری",
        description="دریافت تنظیمات کلی سیستم شامل قیمت هر پخش و غیره.",
        responses={200: AdminPlayConfigurationSerializer}
    )
    def get(self, request):
        config = PlayConfiguration.objects.last()
        if not config:
            config = PlayConfiguration.objects.create()
        serializer = AdminPlayConfigurationSerializer(config)
        return Response(serializer.data)

    @extend_schema(
        summary="بروزرسانی تنظیمات",
        description="تغییر تنظیمات کلی سیستم.",
        request=AdminPlayConfigurationSerializer,
        responses={200: AdminPlayConfigurationSerializer}
    )
    def post(self, request):
        config = PlayConfiguration.objects.last()
        if not config:
            config = PlayConfiguration.objects.create()
        
        serializer = AdminPlayConfigurationSerializer(config, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminBannerAdListView(APIView):
    """List and create banner ads for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="لیست تبلیغات بنری",
        description="دریافت لیست تمامی بنرهای تبلیغاتی.",
        responses={200: AdminBannerAdSerializer(many=True)}
    )
    def get(self, request):
        ads = BannerAd.objects.all().order_by('-created_at')
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(ads, request)
        serializer = AdminBannerAdSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @extend_schema(
        summary="ایجاد تبلیغ بنری جدید",
        description="آپلود تصویر و ایجاد یک بنر تبلیغاتی جدید.",
        request=AdminBannerAdSerializer,
        responses={201: AdminBannerAdSerializer}
    )
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


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminBannerAdDetailView(APIView):
    """Retrieve, update or delete a banner ad for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="جزئیات تبلیغ بنری",
        description="دریافت اطلاعات یک بنر تبلیغاتی خاص.",
        responses={200: AdminBannerAdSerializer}
    )
    def get(self, request, pk):
        ad = get_object_or_404(BannerAd, pk=pk)
        serializer = AdminBannerAdSerializer(ad)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش تبلیغ بنری",
        description="ویرایش اطلاعات یا تصویر یک بنر تبلیغاتی.",
        request=AdminBannerAdSerializer,
        responses={200: AdminBannerAdSerializer}
    )
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

    @extend_schema(
        summary="حذف تبلیغ بنری",
        description="حذف یک بنر تبلیغاتی از سیستم.",
        responses={204: None}
    )
    def delete(self, request, pk):
        ad = get_object_or_404(BannerAd, pk=pk)
        ad.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminAudioAdListView(APIView):
    """List and create audio ads for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="لیست تبلیغات صوتی",
        description="دریافت لیست تمامی تبلیغات صوتی.",
        responses={200: AdminAudioAdSerializer(many=True)}
    )
    def get(self, request):
        ads = AudioAd.objects.all().order_by('-created_at')
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(ads, request)
        serializer = AdminAudioAdSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @extend_schema(
        summary="ایجاد تبلیغ صوتی جدید",
        description="آپلود فایل صوتی و کاور برای ایجاد یک تبلیغ صوتی جدید.",
        request=AdminAudioAdSerializer,
        responses={201: AdminAudioAdSerializer}
    )
    def post(self, request):
        data = request.data.copy()
        # Accept either `file` (flat form-data) or legacy `audio_upload` field
        audio_file = request.FILES.get('file') or request.FILES.get('audio_upload')
        presigned_url = None
        if audio_file:
            safe_title = "".join([c for c in data.get('title', 'audio_ad') if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            filename = f"audio_ad_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}"
            audio_url, _ = upload_file_to_r2(audio_file, folder='ads/audio', custom_filename=filename)
            data['audio_url'] = audio_url

            # generate a presigned (signed) URL for immediate use/testing
            try:
                presigned_url = generate_signed_r2_url(audio_url, expiration=3600)
            except Exception:
                presigned_url = None

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
            response_data = serializer.data
            # include uploaded URLs when available
            if data.get('audio_url'):
                response_data['original_url'] = data.get('audio_url')
                response_data['presigned_url'] = presigned_url
            return Response(response_data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminAudioAdDetailView(APIView):
    """Retrieve, update or delete an audio ad for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="جزئیات تبلیغ صوتی",
        description="دریافت اطلاعات یک تبلیغ صوتی خاص.",
        responses={200: AdminAudioAdSerializer}
    )
    def get(self, request, pk):
        ad = get_object_or_404(AudioAd, pk=pk)
        serializer = AdminAudioAdSerializer(ad)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش تبلیغ صوتی",
        description="ویرایش اطلاعات، فایل صوتی یا کاور یک تبلیغ صوتی.",
        request=AdminAudioAdSerializer,
        responses={200: AdminAudioAdSerializer}
    )
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

    @extend_schema(
        summary="حذف تبلیغ صوتی",
        description="حذف یک تبلیغ صوتی از سیستم.",
        responses={204: None}
    )
    def delete(self, request, pk):
        ad = get_object_or_404(AudioAd, pk=pk)
        ad.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminAlbumListView(APIView):
    """List albums for admin."""
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="لیست آلبوم‌ها",
        description="دریافت لیست تمامی آلبوم‌ها (به جز تک‌آهنگ‌ها) با قابلیت صفحه‌بندی.",
        responses={200: AdminAlbumSerializer(many=True)}
    )
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


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminAlbumDetailView(APIView):
    """Retrieve, update or delete an album for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="جزئیات آلبوم",
        description="دریافت اطلاعات کامل یک آلبوم خاص.",
        responses={200: AdminAlbumSerializer}
    )
    def get(self, request, pk):
        album = get_object_or_404(Album, pk=pk)
        serializer = AdminAlbumSerializer(album)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش جزئی آلبوم",
        description="ویرایش برخی از فیلدهای آلبوم و آپلود کاور جدید.",
        request=AdminAlbumSerializer,
        responses={200: AdminAlbumSerializer}
    )
    def patch(self, request, pk):
        album = get_object_or_404(Album, pk=pk)
        return self._update_album(request, album, partial=True)

    @extend_schema(
        summary="ویرایش کامل آلبوم",
        description="ویرایش تمامی فیلدهای آلبوم.",
        request=AdminAlbumSerializer,
        responses={200: AdminAlbumSerializer}
    )
    def put(self, request, pk):
        album = get_object_or_404(Album, pk=pk)
        return self._update_album(request, album, partial=False)

    @extend_schema(
        summary="حذف آلبوم",
        description="حذف کامل یک آلبوم از سیستم.",
        responses={204: None}
    )
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


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminAlbumSongActionView(APIView):
    """Actions on songs within an album: remove from album or delete song."""
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="عملیات روی آهنگ‌های آلبوم",
        description="حذف آهنگ از آلبوم یا حذف کامل آهنگ از سیستم.",
        request=inline_serializer(
            name='AdminAlbumSongActionRequest',
            fields={'action': serializers.ChoiceField(choices=['remove', 'delete'])}
        ),
        responses={
            200: inline_serializer(
                name='AdminAlbumSongActionResponse',
                fields={'message': serializers.CharField()}
            )
        }
    )
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


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminFinanceSummaryView(APIView):
    """Summary of payments and deposit requests."""
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="خلاصه وضعیت مالی",
        description="دریافت آمار پرداخت‌ها و درخواست‌های تسویه حساب در بازه‌های زمانی مختلف.",
        parameters=[
            OpenApiParameter("start", OpenApiTypes.STR, description="تاریخ شروع (YYYY-MM-DD)"),
            OpenApiParameter("end", OpenApiTypes.STR, description="تاریخ پایان (YYYY-MM-DD)")
        ],
        responses={
            200: inline_serializer(
                name='AdminFinanceSummaryResponse',
                fields={
                    'today': inline_serializer(
                        name='FinanceStats',
                        fields={
                            'total_payments': serializers.FloatField(),
                            'total_deposits': serializers.FloatField(),
                            'count_payments': serializers.IntegerField(),
                            'count_deposits': serializers.IntegerField(),
                        }
                    ),
                    'last_7_days': serializers.DictField(),
                    'last_30_days': serializers.DictField(),
                    'all_time': serializers.DictField(),
                    'custom_period': serializers.DictField(required=False),
                    'custom_period_error': serializers.CharField(required=False),
                }
            )
        }
    )
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


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminPaymentTransactionListView(APIView):
    """List payment transactions with filtering."""
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="لیست تراکنش‌های پرداخت",
        description="دریافت لیست تمامی تراکنش‌های پرداخت با قابلیت فیلتر بر اساس وضعیت.",
        parameters=[
            OpenApiParameter("status", OpenApiTypes.STR, description="وضعیت تراکنش")
        ],
        responses={200: AdminPaymentTransactionSerializer(many=True)}
    )
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


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminDepositRequestListView(APIView):
    """List deposit requests with filtering."""
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="لیست درخواست‌های تسویه",
        description="دریافت لیست تمامی درخواست‌های تسویه حساب هنرمندان با قابلیت فیلتر بر اساس وضعیت.",
        parameters=[
            OpenApiParameter("status", OpenApiTypes.STR, description="وضعیت درخواست")
        ],
        responses={200: AdminDepositRequestSerializer(many=True)}
    )
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


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminSearchSectionListView(APIView):
    """List and create search sections for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="لیست بخش‌های جستجو",
        description="دریافت لیست تمامی بخش‌های (کتگوری‌های) صفحه جستجو.",
        responses={200: AdminSearchSectionSerializer(many=True)}
    )
    def get(self, request):
        sections = SearchSection.objects.all().order_by('-created_at')
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(sections, request)
        serializer = AdminSearchSectionSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @extend_schema(
        summary="ایجاد بخش جستجوی جدید",
        description="ایجاد یک بخش جدید برای صفحه جستجو همراه با آیکون.",
        request=AdminSearchSectionSerializer,
        responses={201: AdminSearchSectionSerializer}
    )
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


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminSearchSectionDetailView(APIView):
    """Retrieve, update or delete a search section for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="جزئیات بخش جستجو",
        description="دریافت اطلاعات یک بخش خاص از صفحه جستجو.",
        responses={200: AdminSearchSectionSerializer}
    )
    def get(self, request, pk):
        section = get_object_or_404(SearchSection, pk=pk)
        serializer = AdminSearchSectionSerializer(section)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش بخش جستجو",
        description="ویرایش اطلاعات یا آیکون یک بخش از صفحه جستجو.",
        request=AdminSearchSectionSerializer,
        responses={200: AdminSearchSectionSerializer}
    )
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

    @extend_schema(
        summary="حذف بخش جستجو",
        description="حذف یک بخش از صفحه جستجو.",
        responses={204: None}
    )
    def delete(self, request, pk):
        section = get_object_or_404(SearchSection, pk=pk)
        section.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminEventPlaylistListView(APIView):
    """List and create event playlist groups for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="لیست گروه‌های پلی‌لیست رویداد",
        description="دریافت لیست تمامی گروه‌های پلی‌لیست مربوط به رویدادها.",
        responses={200: AdminEventPlaylistSerializer(many=True)}
    )
    def get(self, request):
        events = EventPlaylist.objects.all().order_by('-created_at')
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(events, request)
        serializer = AdminEventPlaylistSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @extend_schema(
        summary="ایجاد گروه پلی‌لیست رویداد جدید",
        description="ایجاد یک گروه جدید برای پلی‌لیست‌های رویداد همراه با کاور.",
        request=AdminEventPlaylistSerializer,
        responses={201: AdminEventPlaylistSerializer}
    )
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


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminEventPlaylistDetailView(APIView):
    """Retrieve, update or delete an event playlist group for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="جزئیات گروه پلی‌لیست رویداد",
        description="دریافت اطلاعات یک گروه پلی‌لیست رویداد خاص.",
        responses={200: AdminEventPlaylistSerializer}
    )
    def get(self, request, pk):
        event = get_object_or_404(EventPlaylist, pk=pk)
        serializer = AdminEventPlaylistSerializer(event)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش گروه پلی‌لیست رویداد",
        description="ویرایش اطلاعات یا کاور یک گروه پلی‌لیست رویداد.",
        request=AdminEventPlaylistSerializer,
        responses={200: AdminEventPlaylistSerializer}
    )
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

    @extend_schema(
        summary="حذف گروه پلی‌لیست رویداد",
        description="حذف یک گروه پلی‌لیست رویداد از سیستم.",
        responses={204: None}
    )
    def delete(self, request, pk):
        event = get_object_or_404(EventPlaylist, pk=pk)
        event.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminPlaylistListView(APIView):
    """List and create playlists for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="لیست پلی‌لیست‌های ادمین",
        description="دریافت لیست تمامی پلی‌لیست‌های ایجاد شده توسط ادمین.",
        responses={200: AdminPlaylistSerializer(many=True)}
    )
    def get(self, request):
        playlists = Playlist.objects.all().order_by('-created_at')
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(playlists, request)
        serializer = AdminPlaylistSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @extend_schema(
        summary="ایجاد پلی‌لیست جدید توسط ادمین",
        description="ایجاد یک پلی‌لیست جدید همراه با کاور توسط ادمین.",
        request=AdminPlaylistSerializer,
        responses={201: AdminPlaylistSerializer}
    )
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


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminPlaylistDetailView(APIView):
    """Retrieve, update or delete a playlist for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="جزئیات پلی‌لیست ادمین",
        description="دریافت اطلاعات کامل یک پلی‌لیست خاص.",
        responses={200: AdminPlaylistSerializer}
    )
    def get(self, request, pk):
        playlist = get_object_or_404(Playlist, pk=pk)
        serializer = AdminPlaylistSerializer(playlist)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش پلی‌لیست ادمین",
        description="ویرایش اطلاعات یا کاور یک پلی‌لیست.",
        request=AdminPlaylistSerializer,
        responses={200: AdminPlaylistSerializer}
    )
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

    @extend_schema(
        summary="حذف پلی‌لیست ادمین",
        description="حذف یک پلی‌لیست از سیستم.",
        responses={204: None}
    )
    def delete(self, request, pk):
        playlist = get_object_or_404(Playlist, pk=pk)
        playlist.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminEmployeeListView(APIView):
    """List and create employees (managers/supervisors)."""
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="لیست کارمندان",
        description="دریافت لیست تمامی کارمندان (مدیران و ناظران) سیستم.",
        responses={200: AdminEmployeeSerializer(many=True)}
    )
    def get(self, request):
        # Filter users with manager or supervisor roles who are not staff
        queryset = User.objects.filter(
            Q(roles__contains=User.ROLE_MANAGER) | Q(roles__contains=User.ROLE_SUPERVISOR),
            is_staff=False
        ).order_by('-date_joined')
        
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(queryset, request)
        serializer = AdminEmployeeSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @extend_schema(
        summary="ایجاد کارمند جدید",
        description="ایجاد یک کاربر جدید با نقش مدیر یا ناظر.",
        request=AdminEmployeeSerializer,
        responses={201: AdminEmployeeSerializer}
    )
    def post(self, request):
        serializer = AdminEmployeeSerializer(data=request.data)
        if serializer.is_valid():
            # Ensure is_staff is False and roles are restricted to manager/supervisor
            roles = serializer.validated_data.get('roles', [])
            if not any(role in [User.ROLE_MANAGER, User.ROLE_SUPERVISOR] for role in roles):
                return Response({"error": "User must have manager or supervisor role."}, status=status.HTTP_400_BAD_REQUEST)
            
            serializer.save(is_staff=False)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminEmployeeDetailView(APIView):
    """Retrieve, update or delete an employee."""
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="جزئیات کارمند",
        description="دریافت اطلاعات کامل یک کارمند خاص.",
        responses={200: AdminEmployeeSerializer}
    )
    def get(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        serializer = AdminEmployeeSerializer(user)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش کارمند",
        description="ویرایش اطلاعات یا نقش‌های یک کارمند.",
        request=AdminEmployeeSerializer,
        responses={200: AdminEmployeeSerializer}
    )
    def patch(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        serializer = AdminEmployeeSerializer(user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف کارمند",
        description="حذف یک کارمند از سیستم.",
        responses={204: None}
    )
    def delete(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def get(self, request, pk):
        user = get_object_or_404(User, pk=pk, is_staff=False)
        if not any(role in [User.ROLE_MANAGER, User.ROLE_SUPERVISOR] for role in (user.roles or [])):
            return Response({"error": "Not an employee."}, status=status.HTTP_404_NOT_FOUND)
        serializer = AdminEmployeeSerializer(user)
        return Response(serializer.data)

    def patch(self, request, pk):
        user = get_object_or_404(User, pk=pk, is_staff=False)
        serializer = AdminEmployeeSerializer(user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        user = get_object_or_404(User, pk=pk, is_staff=False)
        user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

