from pathlib import Path
import os

from corsheaders.defaults import default_headers

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get('DJANGO_SECRET', 'changeme-in-production')

# Read debug flag from environment (defaults to True for local development)
DEBUG = os.environ.get('DJANGO_DEBUG', 'False').lower() in {'1', 'true', 'yes', 'on'}

# Keep required public and container hosts even when ALLOWED_HOSTS is supplied.
def _csv_setting(name, default=''):
    return [value.strip() for value in os.environ.get(name, default).split(',') if value.strip()]


_REQUIRED_ALLOWED_HOSTS = [
    '.sedabox.com', 'api.sedabox.com', 'sedabox.com', 'www.sedabox.com',
    '141.11.123.238', '141.11.187.161', 'localhost', '127.0.0.1',
    'web', 'soundbox_web', 'nginx', 'host.docker.internal',
]
ALLOWED_HOSTS = list(dict.fromkeys(_REQUIRED_ALLOWED_HOSTS + _csv_setting('ALLOWED_HOSTS')))

# Browser WebSocket origins are validated separately from the HTTP Host header.
# Keep production origins explicit and permit the local Next.js development
# server. Additional deployment origins can be supplied as a comma-separated
# WEBSOCKET_ALLOWED_ORIGINS environment variable.
_REQUIRED_WEBSOCKET_ALLOWED_ORIGINS = [
    'https://sedabox.com',
    'https://www.sedabox.com',
    'https://api.sedabox.com',
    'http://localhost:3000',
    'http://127.0.0.1:3000',
]
WEBSOCKET_ALLOWED_ORIGINS = list(dict.fromkeys(
    _REQUIRED_WEBSOCKET_ALLOWED_ORIGINS + _csv_setting('WEBSOCKET_ALLOWED_ORIGINS')
))
PUBLIC_API_BASE_URL = os.environ.get('PUBLIC_API_BASE_URL', 'https://api.sedabox.com').rstrip('/')

INSTALLED_APPS = [
    'daphne',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    'rest_framework',
    'drf_spectacular',
    'corsheaders',
    'api',
    'rest_framework_simplejwt.token_blacklist',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'api.middleware.RejectProxyConnectMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.locale.LocaleMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'api.middleware.ArtistPanelSignedR2Middleware',
    'api.middleware.AdminPanelSignedR2Middleware',
    'api.middleware.ClientHomeCacheControlMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'soundbox.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'soundbox.wsgi.application'
ASGI_APPLICATION = 'soundbox.asgi.application'


DATABASES = {
    'default': {
        'ENGINE': os.environ.get('DB_ENGINE', 'django.db.backends.postgresql'),
        'NAME': os.environ.get('DB_NAME', 'soundbox_db'),
        'USER': os.environ.get('DB_USER', 'soundbox_user'),
        'PASSWORD': os.environ.get('DB_PASSWORD', 'postgres'),
        'HOST': os.environ.get('DB_HOST', 'db'),
        'PORT': os.environ.get('DB_PORT', '5432'),
        'CONN_MAX_AGE': int(os.environ.get('DB_CONN_MAX_AGE', '0')),
    }
}


REDIS_URL = os.environ.get('REDIS_URL', 'redis://redis:6379/1')
CHANNEL_REDIS_URL = os.environ.get('CHANNEL_REDIS_URL', REDIS_URL)
REDIS_CONNECT_TIMEOUT = float(os.environ.get('REDIS_CONNECT_TIMEOUT', '1'))
REDIS_SOCKET_TIMEOUT = float(os.environ.get('REDIS_SOCKET_TIMEOUT', '1'))
REDIS_MAX_CONNECTIONS = int(os.environ.get('REDIS_MAX_CONNECTIONS', '96'))
REDIS_RETRY_SECONDS = float(os.environ.get('REDIS_RETRY_SECONDS', '5'))
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            'hosts': [CHANNEL_REDIS_URL],
            'prefix': os.environ.get('CHANNEL_PREFIX', 'sedabox'),
            # Notifications are tiny and user-specific; bounded capacity avoids
            # unbounded memory growth if a client disappears without closing.
            'capacity': int(os.environ.get('CHANNEL_CAPACITY', '100')),
            'expiry': int(os.environ.get('CHANNEL_MESSAGE_EXPIRY', '60')),
            'group_expiry': int(os.environ.get('CHANNEL_GROUP_EXPIRY', '86400')),
        },
    },
}
CACHES = {
    'default': {
        'BACKEND': 'api.cache_backend.ResilientRedisCache',
        'LOCATION': REDIS_URL,
        'TIMEOUT': 300,
        'KEY_PREFIX': 'sedabox',
        'OPTIONS': {
            'socket_connect_timeout': REDIS_CONNECT_TIMEOUT,
            'socket_timeout': REDIS_SOCKET_TIMEOUT,
            'max_connections': REDIS_MAX_CONNECTIONS,
        },
    }
}
CACHE_TTL_HOME = int(os.environ.get('CACHE_TTL_HOME', '90'))
CACHE_TTL_USER_HOME = int(os.environ.get('CACHE_TTL_USER_HOME', '30'))
CACHE_TTL_PLAYLISTS = int(os.environ.get('CACHE_TTL_PLAYLISTS', '120'))
CACHE_TTL_SEARCH = int(os.environ.get('CACHE_TTL_SEARCH', '45'))
CACHE_TTL_USER_SEARCH = int(os.environ.get('CACHE_TTL_USER_SEARCH', '15'))
CACHE_TTL_DISCOVERY = int(os.environ.get('CACHE_TTL_DISCOVERY', '300'))
CACHE_TTL_SIMILAR = int(os.environ.get('CACHE_TTL_SIMILAR', '90'))
SONG_PLAY_COUNT_CACHE_TTL = int(os.environ.get('SONG_PLAY_COUNT_CACHE_TTL', '21600'))
R2_MAX_POOL_CONNECTIONS = int(os.environ.get('R2_MAX_POOL_CONNECTIONS', '64'))
DAPHNE_WORKERS = int(os.environ.get('DAPHNE_WORKERS', '0'))
RUNTIME_MAINTENANCE_INTERVAL = int(os.environ.get('RUNTIME_MAINTENANCE_INTERVAL', '900'))
TRENDING_REFRESH_INTERVAL = int(os.environ.get('TRENDING_REFRESH_INTERVAL', '90'))
STREAM_ACCESS_UNUSED_TTL_HOURS = int(os.environ.get('STREAM_ACCESS_UNUSED_TTL_HOURS', '720'))
STREAM_ACCESS_ABANDONED_TTL_DAYS = int(os.environ.get('STREAM_ACCESS_ABANDONED_TTL_DAYS', '14'))
STREAM_ACCESS_USED_TTL_DAYS = int(os.environ.get('STREAM_ACCESS_USED_TTL_DAYS', '14'))
STREAM_GRANT_MAX_AGE_SECONDS = int(os.environ.get('STREAM_GRANT_MAX_AGE_SECONDS', str(30 * 24 * 60 * 60)))
RUNTIME_CLEANUP_MAX_ROWS_PER_TABLE = int(os.environ.get('RUNTIME_CLEANUP_MAX_ROWS_PER_TABLE', '5000'))
RECOMMENDATION_BACKGROUND_WAIT_MS = int(os.environ.get('RECOMMENDATION_BACKGROUND_WAIT_MS', '150'))
RECOMMENDATION_REFRESH_VERSION_TTL = int(os.environ.get('RECOMMENDATION_REFRESH_VERSION_TTL', str(7 * 24 * 60 * 60)))


# Redis-backed recommendation freshness and safe generated-row housekeeping.
GENERATED_PLAYLIST_MAINTENANCE_ENABLED = os.environ.get('GENERATED_PLAYLIST_MAINTENANCE_ENABLED', 'true').lower() in ('1', 'true', 'yes', 'on')
GENERATED_PLAYLIST_UNUSED_TTL = int(os.environ.get('GENERATED_PLAYLIST_UNUSED_TTL', '3600'))
GENERATED_PLAYLIST_CLEANUP_INTERVAL = int(os.environ.get('GENERATED_PLAYLIST_CLEANUP_INTERVAL', '3600'))
GENERATED_PLAYLIST_CLEANUP_BATCH = int(os.environ.get('GENERATED_PLAYLIST_CLEANUP_BATCH', '500'))
OTP_SEND_COOLDOWN_SECONDS = int(os.environ.get('OTP_SEND_COOLDOWN_SECONDS', '60'))
OTP_REQUEST_TIMEOUT_CONNECT = float(os.environ.get('OTP_REQUEST_TIMEOUT_CONNECT', '1.5'))
OTP_REQUEST_TIMEOUT_READ = float(os.environ.get('OTP_REQUEST_TIMEOUT_READ', '3.5'))

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

LANGUAGE_CODE = 'fa'
LANGUAGES = [
    ('fa', 'فارسی'),
    ('en', 'English'),
]
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

USE_X_FORWARDED_HOST = os.environ.get('USE_X_FORWARDED_HOST', 'False').lower() in {'1', 'true', 'yes', 'on'}
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

STATIC_URL = 'static/'
# Directory where collectstatic will gather files inside the container
STATIC_ROOT = os.path.join(BASE_DIR, 'static')

# Media files (user-uploaded files)
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')
SERVE_MEDIA_FILES = os.environ.get('SERVE_MEDIA_FILES', '1').lower() in {'1', 'true', 'yes', 'on'}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# REST framework defaults
REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': [
        'api.localization.LocalizedJSONRenderer',
        'rest_framework.renderers.BrowsableAPIRenderer',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.AllowAny',
    ],
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'api.authentication.OptionalJWTAuthentication',
    ],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'SoundBox API',
    'DESCRIPTION': 'API documentation for SoundBox project',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'SWAGGER_UI_SETTINGS': {
        'defaultModelsExpandDepth': -1,
        'defaultModelExpandDepth': -1,
    },
}

# CORS - allow all origins for now (change in production)
CORS_ALLOW_ALL_ORIGINS = True

# Keep the API compatible with clients that still send the legacy language
# header. New web clients use the CORS-safelisted Accept-Language header and do
# not require this custom entry, but allowing it prevents avoidable preflight
# failures during rolling deployments.
CORS_ALLOW_HEADERS = (*default_headers, "x-app-language")

# CSRF trusted origins for admin panel
_REQUIRED_CSRF_ORIGINS = [
    'https://api.sedabox.com', 'https://www.sedabox.com', 'https://sedabox.com',
    'http://141.11.187.161', 'https://141.11.187.161',
]
CSRF_TRUSTED_ORIGINS = list(dict.fromkeys(
    _REQUIRED_CSRF_ORIGINS + _csv_setting('CSRF_TRUSTED_ORIGINS')
))

# Use custom user model
AUTH_USER_MODEL = 'api.User'

from datetime import timedelta

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=60),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
}

# Cloudflare R2 (S3-compatible) configuration
# For development you can place values directly here, but
# it's recommended to use environment variables in production.
R2_ENDPOINT_URL = 'https://3d69ac7dfa7e78d81da2bd72904fa0a2.r2.cloudflarestorage.com'
R2_ACCESS_KEY_ID = '97a14ac8fa46143cf97744793babd0b3'
R2_SECRET_ACCESS_KEY = '8d458c9a3ec31490a533230ec43a3988d5aabdbb6a6d1f45fa169d04d6054f16'
R2_BUCKET_NAME = 'sedabox'
# CDN base used to build final download URLs. Ensure this matches your CDN configuration.
R2_CDN_BASE = 'https://cdn.sedabox.com'
ARTIST_API_PREFIX = os.environ.get('ARTIST_API_PREFIX', '/api/artist/')
ADMIN_API_PREFIX = os.environ.get('ADMIN_API_PREFIX', '/api/admin/')
ADMIN_R2_SIGNED_URL_TTL = int(os.environ.get('ADMIN_R2_SIGNED_URL_TTL', '3600'))
ARTIST_R2_SIGNED_URL_TTL = int(os.environ.get('ARTIST_R2_SIGNED_URL_TTL', '3600'))
HOME_API_PREFIX = os.environ.get('HOME_API_PREFIX', '/api/home/')
HOME_R2_SIGNED_URL_TTL = int(os.environ.get('HOME_R2_SIGNED_URL_TTL', '3600'))
ARTIST_UPLOAD_RECOVERY_TTL = int(os.environ.get('ARTIST_UPLOAD_RECOVERY_TTL', '3600'))

# SMS / Kavenegar settings
SMS_PROVIDER = 'kavenegar'
# Kavenegar API key (set to your production key)
KAVENEGAR_API_KEY = '705A6B6B64733841377A564A3934726A746E747A547477547233656643624F343467776B572F54315476733D'
APP_NAME = 'Sedabox'

# Logging: ensure INFO logs are visible (so OTP send attempts are logged)
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'root': {'handlers': ['console'], 'level': 'INFO'},
    'loggers': {
        'django.security.DisallowedHost': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}
