from rest_framework import serializers
from .models import User
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer


class UserSerializer(serializers.ModelSerializer):
    followers = serializers.SerializerMethodField()
    followings = serializers.PrimaryKeyRelatedField(queryset=get_user_model().objects.all(), many=True, required=False)
    playlists = serializers.JSONField()
    settings = serializers.JSONField()
    class Meta:
        model = get_user_model()
        fields = [
            'id', 'phone_number', 'first_name', 'last_name', 'email',
            'roles', 'is_active', 'is_staff', 'date_joined',
            'followers', 'followings', 'playlists', 'plan', 'settings'
        ]
        read_only_fields = ['id', 'is_active', 'is_staff', 'date_joined', 'followers']

    def get_followers(self, obj):
        # return minimal follower info: id and phone_number
        return [{'id': u.id, 'phone_number': u.phone_number} for u in obj.followers.all()]


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    playlists = serializers.JSONField(required=False)
    settings = serializers.JSONField(required=False)

    class Meta:
        model = User
        fields = ['phone_number', 'password', 'roles', 'first_name', 'last_name', 'email', 'playlists', 'plan', 'settings']

    def validate_phone_number(self, value):
        if User.objects.filter(phone_number=value).exists():
            raise serializers.ValidationError('A user with that phone number already exists')
        return value

    def create(self, validated_data):
        password = validated_data.pop('password')
        user = User.objects.create_user(password=password, **validated_data)
        return user


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        data = super().validate(attrs)
        # attach user profile but exclude internal flags from the login response
        user_data = UserSerializer(self.user).data
        # remove `is_staff` so it doesn't appear in the token response
        user_data.pop('is_staff', None)
        data['user'] = user_data
        return data
