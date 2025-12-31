from rest_framework import serializers
from .models import User, Artist, ArtistAuth, NotificationSetting
from django.contrib.auth import get_user_model

User = get_user_model()

class AdminUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            'id', 'phone_number', 'first_name', 'last_name', 'email',
            'roles', 'is_active', 'is_staff', 'is_verified', 'date_joined',
            'plan', 'stream_quality', 'last_login_at', 'failed_login_attempts', 'locked_until'
        ]
        read_only_fields = ['id', 'date_joined', 'last_login_at']

class AdminArtistSerializer(serializers.ModelSerializer):
    has_user = serializers.SerializerMethodField()
    user_phone = serializers.CharField(source='user.phone_number', read_only=True)

    class Meta:
        model = Artist
        fields = [
            'id', 'name', 'artistic_name', 'email', 'city', 'date_of_birth',
            'address', 'id_number', 'user', 'user_phone', 'bio', 'profile_image',
            'banner_image', 'verified', 'created_at', 'has_user'
        ]
        read_only_fields = ['id', 'created_at']

    def get_has_user(self, obj):
        return obj.user is not None

class AdminArtistAuthSerializer(serializers.ModelSerializer):
    class Meta:
        model = ArtistAuth
        fields = [
            'id', 'user', 'auth_type', 'artist_claimed', 'first_name', 'last_name', 'stage_name',
            'birth_date', 'national_id', 'phone_number', 'email', 'city', 'address',
            'biography', 'national_id_image', 'status', 'is_verified', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
