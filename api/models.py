from django.db import models
from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)


class UserManager(BaseUserManager):
    def create_user(self, phone_number, password=None, roles='audience', **extra_fields):
        if not phone_number:
            raise ValueError('The phone number must be set')
        phone_number = str(phone_number)
        user = self.model(phone_number=phone_number, roles=roles, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, phone_number, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(phone_number, password=password, roles='admin', **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    ROLE_AUDIENCE = 'audience'
    ROLE_ARTIST = 'artist'
    ROLE_ADMIN = 'admin'

    ROLE_CHOICES = [
        (ROLE_AUDIENCE, 'Audience'),
        (ROLE_ARTIST, 'Artist'),
        (ROLE_ADMIN, 'Admin'),
    ]

    phone_number = models.CharField(max_length=20, unique=True)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    email = models.EmailField(blank=True, null=True)
    roles = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_AUDIENCE)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True)

    # users this user follows; reverse accessor 'followers' gives users following this user
    followings = models.ManyToManyField('self', symmetrical=False, related_name='followers', blank=True)

    # playlists: store as JSON (list of playlist objects or ids); can be replaced with real Playlist model later
    playlists = models.JSONField(default=list, blank=True)

    # current plan for user
    PLAN_FREE = 'free'
    PLAN_PREMIUM = 'premium'
    PLAN_CHOICES = [
        (PLAN_FREE, 'Free'),
        (PLAN_PREMIUM, 'Premium'),
    ]
    plan = models.CharField(max_length=30, choices=PLAN_CHOICES, default=PLAN_FREE)

    # user-specific settings stored as JSON
    settings = models.JSONField(default=dict, blank=True)

    objects = UserManager()

    USERNAME_FIELD = 'phone_number'
    REQUIRED_FIELDS = []

    def __str__(self):
        return self.phone_number
