import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, unquote, urlparse

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


def artist_filename_name(artist) -> str:
    """Use the artist's English stage/name first for stored and downloaded filenames."""
    if not artist:
        return ''
    for field in ('artistic_name_en', 'name_en', 'artistic_name', 'name'):
        value = str(getattr(artist, field, '') or '').strip()
        if value:
            return value
    return ''


def r2_object_key(value, *, allow_key=True):
    """Return the object key for a Sedabox R2 reference, otherwise ``''``."""
    if not value:
        return ''

    text = str(value).strip()
    if not text:
        return ''
    if not text.startswith(('http://', 'https://')):
        return unquote(text.lstrip('/')) if allow_key else ''

    parsed = urlparse(text)
    host = (parsed.hostname or '').lower()
    configured_hosts = {
        (urlparse(getattr(settings, 'R2_CDN_BASE', '')).hostname or '').lower(),
        (urlparse(getattr(settings, 'R2_ENDPOINT_URL', '')).hostname or '').lower(),
    }
    is_r2_host = (
        host in configured_hosts
        or host.endswith('.r2.dev')
        or host.endswith('.r2.cloudflarestorage.com')
    )
    if not host or not is_r2_host:
        return ''

    key = unquote(parsed.path.lstrip('/'))
    bucket = str(getattr(settings, 'R2_BUCKET_NAME', '') or '').strip('/')
    if bucket and key.startswith(f'{bucket}/'):
        key = key[len(bucket) + 1:]
    return key


def _fresh_signed_r2_url(value, minimum_ttl=60):
    """Return whether an R2 presigned URL remains usable beyond ``minimum_ttl``."""
    try:
        query = parse_qs(urlparse(str(value)).query)
        created = datetime.strptime(query['X-Amz-Date'][0], '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        expires = int(query['X-Amz-Expires'][0])
        remaining = created.timestamp() + expires - datetime.now(timezone.utc).timestamp()
        return 'X-Amz-Signature' in query and remaining > minimum_ttl
    except (KeyError, TypeError, ValueError):
        return False


def generate_signed_r2_url(object_key, expiration=3600):
    """Generate a fresh short-lived GET URL for a Sedabox R2 object."""
    key = r2_object_key(object_key)
    if not key:
        return str(object_key) if str(object_key or '').startswith(('http://', 'https://')) else None

    try:
        return _r2_client().generate_presigned_url(
            'get_object',
            Params={'Bucket': settings.R2_BUCKET_NAME, 'Key': key},
            ExpiresIn=max(60, min(int(expiration), 86400)),
        )
    except Exception:
        logger.exception('Could not sign R2 object %s', key)
        return None


def sign_r2_urls_in_payload(value, expiration=3600, *, strict=False, refresh=False):
    """Recursively replace R2 URLs with usable signed URLs.

    ``refresh=True`` deliberately re-signs cached URLs even when their old
    signature has not expired yet. This is used by client-home responses so
    every backend request receives a full fresh validity window.
    """
    if isinstance(value, dict):
        return {
            key: sign_r2_urls_in_payload(
                item, expiration, strict=strict, refresh=refresh
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            sign_r2_urls_in_payload(item, expiration, strict=strict, refresh=refresh)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            sign_r2_urls_in_payload(item, expiration, strict=strict, refresh=refresh)
            for item in value
        )
    if not isinstance(value, str) or not r2_object_key(value, allow_key=False):
        return value
    if not refresh and _fresh_signed_r2_url(value):
        return value

    signed = generate_signed_r2_url(value, expiration=expiration)
    if signed:
        return signed
    if strict:
        raise MediaPipelineError('A private media link could not be authorized.', 'r2_signing_failed', 503)
    return value

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
    return r2_object_key(value)


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
    raw_name = getattr(file_obj, 'name', None)
    if not isinstance(raw_name, (str, bytes, os.PathLike)):
        raw_name = 'upload'
    original_filename = os.path.basename(raw_name or 'upload')
    filename = os.path.basename(custom_filename or original_filename)
    _, custom_ext = os.path.splitext(filename)
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


def upload_audio_variants(file_obj, filename_base, stage_callback=None):
    """Validate audio, create the 128 kbps variant, and upload both files to R2."""
    def notify(stage):
        if not stage_callback:
            return
        try:
            stage_callback(stage)
        except Exception:
            logger.exception('Audio upload stage callback failed at %s', stage)

    notify('analyzing')
    duration, bitrate, audio_format = get_audio_info(file_obj)
    if not duration or audio_format not in {'mp3', 'wav'}:
        raise MediaPipelineError('The audio file is damaged or cannot be decoded.', 'invalid_audio', 400)

    safe_base = make_safe_filename(filename_base) or 'track'
    uploaded = []
    converted = None
    try:
        needs_conversion = audio_format != 'mp3' or bitrate is None or bitrate > 128
        if needs_conversion:
            notify('converting_128')
            converted = convert_to_128kbps(file_obj)

        notify('uploading_r2')
        jobs = {
            'audio_file': (
                file_obj,
                'songs',
                f'{safe_base}.{audio_format}',
            ),
        }
        if converted:
            jobs['converted_audio_url'] = (
                converted,
                'songs/128',
                f'{safe_base}_128.mp3',
            )

        results = {'converted_audio_url': None}
        errors = []
        with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
            futures = {
                executor.submit(
                    upload_file_to_r2,
                    file_value,
                    folder=folder,
                    custom_filename=filename,
                ): field
                for field, (file_value, folder, filename) in jobs.items()
            }
            for future in as_completed(futures):
                field = futures[future]
                try:
                    url, _ = future.result()
                    results[field] = url
                    uploaded.append(url)
                except Exception as exc:
                    errors.append(exc)

        if errors:
            raise errors[0]

        notify('saving')
        return {
            'audio_file': results['audio_file'],
            'converted_audio_url': results['converted_audio_url'],
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

