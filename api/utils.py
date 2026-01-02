import os
import boto3
import mimetypes
import io
from django.conf import settings
from botocore.config import Config
from mutagen.mp3 import MP3
from mutagen.wave import WAVE
from pydub import AudioSegment

def generate_signed_r2_url(object_key, expiration=3600):
    """
    Generate a short-lived signed URL for R2 object.
    """
    if not object_key:
        return None
        
    # If it's already a full URL, extract the key if it's our CDN
    cdn_base = getattr(settings, 'R2_CDN_BASE', 'https://cdn.sedabox.com').rstrip('/')
    if object_key.startswith(cdn_base):
        object_key = object_key.replace(cdn_base + '/', '')
    elif object_key.startswith('http'):
        # Not our CDN or already signed?
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

def upload_file_to_r2(file_obj, folder='', custom_filename=None):
    """
    Helper function to upload a file to R2 and return the CDN URL.
    """
    original_filename = getattr(file_obj, 'name', None) or 'upload'
    
    if custom_filename:
        filename = custom_filename
    else:
        filename = original_filename
    
    print(f"DEBUG: upload_file_to_r2: filename={filename}, folder={folder}")
    
    # Get original format
    _, ext = os.path.splitext(original_filename)
    original_format = ext.lstrip('.').lower()
    
    # Build key
    key = f"{folder + '/' if folder else ''}{filename}"
    print(f"DEBUG: R2 Key: {key}")
    
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
    if hasattr(file_obj, 'seek'):
        file_obj.seek(0)
        
    try:
        s3.upload_fileobj(
            file_obj,
            getattr(settings, 'R2_BUCKET_NAME'),
            key,
            ExtraArgs={'ContentType': content_type}
        )
        print(f"DEBUG: Upload successful")
    except Exception as e:
        print(f"DEBUG: R2 Upload failed: {str(e)}")
        raise e
    
    # Build CDN URL
    cdn_base = getattr(settings, 'R2_CDN_BASE', 'https://cdn.sedabox.com').rstrip('/')
    url = f"{cdn_base}/{key}"
    print(f"DEBUG: Returning URL: {url}")
    
    return url, original_format

def convert_to_128kbps(file_obj):
    """
    Convert an audio file to 128kbps MP3.
    Returns a file-like object.
    """
    print(f"DEBUG: convert_to_128kbps started")
    if hasattr(file_obj, 'seek'):
        file_obj.seek(0)
    
    try:
        print(f"DEBUG: Loading audio with pydub...")
        audio = AudioSegment.from_file(file_obj)
        print(f"DEBUG: Audio loaded. Duration: {len(audio)}ms")
        buffer = io.BytesIO()
        print(f"DEBUG: Exporting to mp3 128k...")
        audio.export(buffer, format="mp3", bitrate="128k")
        buffer.seek(0)
        print(f"DEBUG: Export finished. Buffer size: {buffer.getbuffer().nbytes} bytes")
        return buffer
    except Exception as e:
        print(f"DEBUG: pydub conversion error: {str(e)}")
        raise e

def get_audio_info(file_path_or_obj):
    """
    Extract duration and bitrate from audio file.
    """
    try:
        # If it's a file object, we might need to save it temporarily or use a library that supports file objects
        # Mutagen supports file objects for some formats
        if hasattr(file_path_or_obj, 'seek'):
            file_path_or_obj.seek(0)
            
        # Try MP3
        try:
            audio = MP3(file_path_or_obj)
            duration = int(audio.info.length)
            bitrate = int(audio.info.bitrate / 1000)
            return duration, bitrate, 'mp3'
        except Exception:
            pass
            
        # Try WAV
        if hasattr(file_path_or_obj, 'seek'):
            file_path_or_obj.seek(0)
        try:
            audio = WAVE(file_path_or_obj)
            duration = int(audio.info.length)
            return duration, None, 'wav'
        except Exception:
            pass
            
        return None, None, None
    except Exception:
        return None, None, None

