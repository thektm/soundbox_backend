import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
from urllib.parse import quote, unquote

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings
from mutagen.mp3 import MP3
from mutagen.wave import WAVE

logger = logging.getLogger(__name__)


def absolute_api_url(request, path):
    """Build stable API links without trusting proxy Host/X-Forwarded-Host values."""
    if not path:
        return None
    value = str(path)
    if value.startswith(('http://', 'https://')):
        return value
    base = getattr(settings, 'PUBLIC_API_BASE_URL', '').rstrip('/')
    if base:
        return f"{base}/{value.lstrip('/')}"
    try:
        return request.build_absolute_uri(value) if request else value
    except Exception:
        return value


def public_media_url(request, file_value, version=None):
    """Return a stable, absolute and cache-safe URL for a local media file.

    ``ImageField.url`` is relative when serializers are created without a request
    and may use an internal Docker host when proxy headers are incomplete.  User
    avatars are public assets, so always anchor them to ``PUBLIC_API_BASE_URL``
    and append a version derived from ``updated_at`` to invalidate browser and
    CDN caches after a replacement upload.
    """
    if not file_value:
        return ''

    try:
        raw_url = file_value.url
    except (AttributeError, ValueError):
        raw_url = str(file_value or '')

    if not raw_url:
        return ''

    url = absolute_api_url(request, raw_url) or ''
    if not url:
        return ''

    if version is not None:
        try:
            token = int(version.timestamp())
        except (AttributeError, TypeError, ValueError, OverflowError):
            token = str(version).strip()
        if token:
            separator = '&' if '?' in url else '?'
            url = f"{url}{separator}v={token}"

    return url


def user_profile_image_url(user, request=None, *, include_unpublished=False):
    """Resolve a normal user's current avatar consistently across APIs."""
    if not user:
        return ''

    try:
        profile = user.image_profile
    except Exception:
        profile = None

    if profile and getattr(profile, 'image', None):
        status = getattr(profile, 'status', '')
        if include_unpublished or status == 'published':
            return public_media_url(
                request,
                profile.image,
                version=getattr(profile, 'updated_at', None),
            )

    settings_value = getattr(user, 'settings', None)
    if isinstance(settings_value, dict):
        legacy = settings_value.get('profile_image') or ''
        return absolute_api_url(request, legacy) or ''
    return ''

def make_safe_filename(s: str) -> str:
    """Sanitize a filename base by removing problematic characters and collapsing whitespace."""
    if not s:
        return ''
    allowed = set(' -_.,()')
    cleaned = ''.join(ch for ch in str(s) if ch.isalnum() or ch in allowed)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' .-_')
    return cleaned[:180]

def generate_signed_r2_url(object_key, expiration=3600):
    """
    Generate a short-lived signed URL for R2 object.
    """
    if not object_key:
        return None
        
    # If it's already a full URL, extract the key if it's our CDN or R2 domain
    cdn_base = getattr(settings, 'R2_CDN_BASE', 'https://cdn.sedabox.com').rstrip('/')
    if object_key.startswith(cdn_base):
        object_key = unquote(object_key.replace(cdn_base + '/', ''))
    elif 'r2.cloudflarestorage.com' in object_key or 'r2.dev' in object_key:
        # Extract key from standard R2 structure (everything after the domain)
        parts = object_key.split('/')
        if len(parts) > 3:
            object_key = unquote('/'.join(parts[3:]))
    elif object_key.startswith('http'):
        # External URL, return as is
        return object_key

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
    
    try:
        signed_url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket_name, 'Key': object_key},
            ExpiresIn=expiration
        )
        return signed_url
    except Exception:
        return None

class MediaPipelineError(Exception):
    def __init__(self, message, code='media_pipeline_failed', status_code=502):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _r2_client():
    required = {
        'endpoint': getattr(settings, 'R2_ENDPOINT_URL', None),
        'access key': getattr(settings, 'R2_ACCESS_KEY_ID', None),
        'secret key': getattr(settings, 'R2_SECRET_ACCESS_KEY', None),
        'bucket': getattr(settings, 'R2_BUCKET_NAME', None),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise MediaPipelineError(
            f"R2 storage is not configured ({', '.join(missing)} missing).",
            'r2_not_configured',
            503,
        )
    kwargs = {
        'service_name': 's3',
        'endpoint_url': required['endpoint'],
        'aws_access_key_id': required['access key'],
        'aws_secret_access_key': required['secret key'],
        'config': Config(
            signature_version='s3v4',
            connect_timeout=10,
            read_timeout=180,
            retries={'max_attempts': 3, 'mode': 'standard'},
        ),
    }
    token = getattr(settings, 'R2_SESSION_TOKEN', None)
    if token:
        kwargs['aws_session_token'] = token
    return boto3.client(**kwargs)


def _r2_key(value):
    if not value:
        return ''
    value = str(value)
    cdn_base = getattr(settings, 'R2_CDN_BASE', 'https://cdn.sedabox.com').rstrip('/')
    if value.startswith(cdn_base + '/'):
        return unquote(value[len(cdn_base) + 1:])
    if 'r2.cloudflarestorage.com' in value or 'r2.dev' in value:
        parts = value.split('/', 3)
        return unquote(parts[3]) if len(parts) == 4 else ''
    return value if not value.startswith(('http://', 'https://')) else ''


def delete_file_from_r2(value):
    key = _r2_key(value)
    if not key:
        return False
    try:
        _r2_client().delete_object(Bucket=settings.R2_BUCKET_NAME, Key=key)
        return True
    except Exception:
        logger.exception('Could not delete R2 object %s', key)
        return False


def cleanup_r2_urls(values):
    for value in dict.fromkeys(item for item in values if item):
        delete_file_from_r2(value)


def upload_file_to_r2(file_obj, folder='', custom_filename=None, bitrate_label=None, check_existing=True):
    """Upload a file to R2 and return ``(cdn_url, original_extension)``."""
    original_filename = os.path.basename(getattr(file_obj, 'name', None) or 'upload')
    filename = os.path.basename(custom_filename or original_filename)
    custom_base, custom_ext = os.path.splitext(filename)
    _, original_ext = os.path.splitext(original_filename)
    if not custom_ext and original_ext:
        filename += original_ext
    if bitrate_label:
        base, ext = os.path.splitext(filename)
        filename = f'{base}({bitrate_label}){ext}'

    folder = str(folder or '').strip('/')
    key = f'{folder}/{filename}' if folder else filename
    client = _r2_client()
    bucket = settings.R2_BUCKET_NAME

    if check_existing:
        base_key = key
        counter = 1
        while True:
            try:
                client.head_object(Bucket=bucket, Key=key)
                base, ext = os.path.splitext(base_key)
                key = f'{base}-{counter}{ext}'
                counter += 1
            except ClientError as exc:
                code = str(exc.response.get('Error', {}).get('Code', ''))
                if code in {'404', 'NoSuchKey', 'NotFound'}:
                    break
                raise MediaPipelineError('R2 could not verify the destination file.', 'r2_lookup_failed') from exc

    content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    if hasattr(file_obj, 'seek'):
        file_obj.seek(0)
    try:
        client.upload_fileobj(
            file_obj,
            bucket,
            key,
            ExtraArgs={'ContentType': content_type},
            Config=TransferConfig(
                multipart_threshold=8 * 1024 * 1024,
                multipart_chunksize=8 * 1024 * 1024,
                max_concurrency=4,
                use_threads=True,
            ),
        )
    except MediaPipelineError:
        raise
    except Exception as exc:
        logger.exception('R2 upload failed for %s', key)
        raise MediaPipelineError('R2 upload failed. Please retry the file.', 'r2_upload_failed') from exc

    cdn_base = getattr(settings, 'R2_CDN_BASE', 'https://cdn.sedabox.com').rstrip('/')
    return f'{cdn_base}/{quote(key, safe="/")}', original_ext.lstrip('.').lower()


def convert_to_128kbps(file_obj):
    """Transcode to 128 kbps MP3 with ffmpeg without loading the full file in RAM."""
    source_path = None
    temporary_source = False
    output_path = None
    try:
        try:
            source_path = file_obj.temporary_file_path()
        except (AttributeError, OSError):
            suffix = os.path.splitext(getattr(file_obj, 'name', '') or '')[1] or '.audio'
            source = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            source_path, temporary_source = source.name, True
            if hasattr(file_obj, 'seek'):
                file_obj.seek(0)
            with source:
                shutil.copyfileobj(file_obj, source, length=1024 * 1024)

        fd, output_path = tempfile.mkstemp(suffix='.mp3')
        os.close(fd)
        process = subprocess.run(
            [
                'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                '-i', source_path, '-vn', '-map_metadata', '-1',
                '-codec:a', 'libmp3lame', '-b:a', '128k', output_path,
            ],
            capture_output=True,
            timeout=int(getattr(settings, 'AUDIO_TRANSCODE_TIMEOUT_SECONDS', 900)),
            check=False,
        )
        if process.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            logger.error('ffmpeg conversion failed: %s', process.stderr.decode(errors='ignore')[-1000:])
            raise MediaPipelineError('The 128 kbps audio version could not be created.', 'audio_conversion_failed')

        result = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode='w+b')
        with open(output_path, 'rb') as converted:
            shutil.copyfileobj(converted, result, length=1024 * 1024)
        result.seek(0)
        return result
    except subprocess.TimeoutExpired as exc:
        raise MediaPipelineError('Audio processing timed out. Try a smaller or valid audio file.', 'audio_conversion_timeout', 504) from exc
    except FileNotFoundError as exc:
        raise MediaPipelineError('Audio processing is unavailable because ffmpeg is not installed.', 'ffmpeg_missing', 503) from exc
    finally:
        if hasattr(file_obj, 'seek'):
            try:
                file_obj.seek(0)
            except Exception:
                pass
        if temporary_source and source_path:
            try:
                os.remove(source_path)
            except OSError:
                pass
        if output_path:
            try:
                os.remove(output_path)
            except OSError:
                pass


def get_audio_info(file_path_or_obj):
    """Return ``(duration_seconds, bitrate_kbps, format)`` for MP3/WAV files."""
    try:
        if hasattr(file_path_or_obj, 'seek'):
            file_path_or_obj.seek(0)
        try:
            audio = MP3(file_path_or_obj)
            return max(1, round(audio.info.length)), max(1, round(audio.info.bitrate / 1000)), 'mp3'
        except Exception:
            pass

        if hasattr(file_path_or_obj, 'seek'):
            file_path_or_obj.seek(0)
        try:
            audio = WAVE(file_path_or_obj)
            bitrate = getattr(audio.info, 'bitrate', None)
            return max(1, round(audio.info.length)), round(bitrate / 1000) if bitrate else None, 'wav'
        except Exception:
            return None, None, None
    finally:
        if hasattr(file_path_or_obj, 'seek'):
            try:
                file_path_or_obj.seek(0)
            except Exception:
                pass


def upload_audio_variants(file_obj, filename_base):
    """Upload the source and, when source quality exceeds 128 kbps, a 128 kbps MP3."""
    duration, bitrate, audio_format = get_audio_info(file_obj)
    if not duration or audio_format not in {'mp3', 'wav'}:
        raise MediaPipelineError('The audio file is damaged or cannot be decoded.', 'invalid_audio', 400)

    safe_base = make_safe_filename(filename_base) or 'track'
    uploaded = []
    converted = None
    try:
        original_url, _ = upload_file_to_r2(
            file_obj,
            folder='songs',
            custom_filename=f'{safe_base}.{audio_format}',
        )
        uploaded.append(original_url)

        converted_url = None
        if audio_format != 'mp3' or bitrate is None or bitrate > 128:
            converted = convert_to_128kbps(file_obj)
            converted_url, _ = upload_file_to_r2(
                converted,
                folder='songs/128',
                custom_filename=f'{safe_base}_128.mp3',
            )
            uploaded.append(converted_url)

        return {
            'audio_file': original_url,
            'converted_audio_url': converted_url,
            'duration_seconds': duration,
            'original_format': audio_format,
            'source_bitrate_kbps': bitrate,
        }
    except Exception:
        cleanup_r2_urls(uploaded)
        raise
    finally:
        if converted:
            converted.close()
        if hasattr(file_obj, 'seek'):
            try:
                file_obj.seek(0)
            except Exception:
                pass
