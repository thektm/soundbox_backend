from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from rest_framework.pagination import PageNumberPagination
from django.shortcuts import get_object_or_404
from .models import User, Artist, ArtistAuth
from .models import PlayCount
from django.utils import timezone
from datetime import timedelta
from .admin_serializers import AdminUserSerializer, AdminArtistSerializer, AdminArtistAuthSerializer
from rest_framework.parsers import MultiPartParser, FormParser

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


class AdminStreamSummaryView(APIView):
        """Return overall play counts summary for admin dashboards.

        Response:
        {
            "total": int,
            "last_30_days": int,
            "last_7_days": int,
            "last_24_hours": int
        }
        """
        permission_classes = [permissions.IsAdminUser]

        def get(self, request):
                now = timezone.now()
                last_24 = now - timedelta(days=1)
                last_7 = now - timedelta(days=7)
                last_30 = now - timedelta(days=30)

                total = PlayCount.objects.count()
                last_30_count = PlayCount.objects.filter(created_at__gte=last_30).count()
                last_7_count = PlayCount.objects.filter(created_at__gte=last_7).count()
                last_24_count = PlayCount.objects.filter(created_at__gte=last_24).count()

                return Response({
                        'total': total,
                        'last_30_days': last_30_count,
                        'last_7_days': last_7_count,
                        'last_24_hours': last_24_count,
                })
