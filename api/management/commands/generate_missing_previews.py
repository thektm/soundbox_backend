import io
import os
import subprocess
import time
from contextlib import contextmanager

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.models import F, Q
from django.utils import timezone
from datetime import timedelta

from api.models import Song
from api.utils import generate_signed_r2_url, upload_file_to_r2


LOCK_ID = 739_301_530
PREVIEW_SECONDS = 30
FFMPEG_THREADS = max(1, int(os.environ.get('PREVIEW_FFMPEG_THREADS', '1')))


@contextmanager
def preview_generation_lock():
    """Use one generator across all web containers."""
    acquired = True
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
            acquired = bool(cursor.fetchone()[0])
    try:
        yield acquired
    finally:
        if acquired and connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])


def create_preview(source_url: str) -> io.BytesIO:
    """Read the source remotely and keep only the 30-second result in RAM."""
    remote_url = generate_signed_r2_url(source_url, expiration=900) or source_url
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        remote_url,
        "-t",
        str(PREVIEW_SECONDS),
        "-map",
        "0:a:0",
        "-vn",
        "-map_metadata",
        "-1",
        "-ac",
        "2",
        "-ar",
        "44100",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "96k",
        "-threads",
        str(FFMPEG_THREADS),
        "-f",
        "mp3",
        "pipe:1",
    ]
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        error = result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(error[-2000:] or "FFmpeg produced no preview")
    preview = io.BytesIO(result.stdout)
    preview.name = "preview.mp3"
    preview.seek(0)
    return preview


class Command(BaseCommand):
    help = "Generate missing 30-second guest previews without storing source files locally"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument(
            "--retry-errors",
            action="store_true",
            help="Ignore retry cooldown and attempt limits for a manual recovery run.",
        )
        parser.add_argument("--max-attempts", type=int, default=5)
        parser.add_argument("--retry-cooldown-hours", type=int, default=6)
        parser.add_argument("--attempts-per-run", type=int, default=3)

    def handle(self, *args, **options):
        published = Song.objects.filter(status=Song.STATUS_PUBLISHED)
        missing = published.filter(
            Q(preview_audio_url__isnull=True) | Q(preview_audio_url="")
        ).exclude(Q(audio_file__isnull=True) | Q(audio_file=""))

        missing_total = missing.count()
        if not options["retry_errors"]:
            retry_before = timezone.now() - timedelta(hours=max(1, options["retry_cooldown_hours"]))
            missing = missing.filter(
                preview_attempts__lt=max(1, options["max_attempts"])
            ).filter(
                Q(preview_last_attempt_at__isnull=True)
                | Q(preview_last_attempt_at__lte=retry_before)
            )

        total_count = published.count()
        preview_count = published.exclude(
            Q(preview_audio_url__isnull=True) | Q(preview_audio_url="")
        ).count()
        eligible_count = missing.count()
        deferred_count = max(0, missing_total - eligible_count)
        self.stdout.write(
            "Preview audit: "
            f"published={total_count}, ready={preview_count}, missing={missing_total}, "
            f"eligible_now={eligible_count}, deferred={deferred_count}"
        )
        if eligible_count == 0:
            self.stdout.write(self.style.SUCCESS("No previews need generation."))
            return

        with preview_generation_lock() as acquired:
            if not acquired:
                self.stdout.write(
                    self.style.WARNING("Another container is generating previews; skipping.")
                )
                return

            queryset = missing.select_related("artist").order_by("id")
            if options["limit"]:
                queryset = queryset[: options["limit"]]

            generated = failed = 0
            attempts_per_run = max(1, options["attempts_per_run"])
            for song in queryset.iterator(chunk_size=20):
                source_url = song.converted_audio_url or song.audio_file
                final_error = None
                succeeded = False

                for local_attempt in range(1, attempts_per_run + 1):
                    preview = None
                    try:
                        preview = create_preview(source_url)
                        filename = f"song-{song.pk}-preview-30s.mp3"
                        preview.name = filename
                        preview_url, _ = upload_file_to_r2(
                            preview,
                            folder="previews",
                            custom_filename=filename,
                            check_existing=False,
                        )
                        Song.objects.filter(pk=song.pk).update(
                            preview_audio_url=preview_url,
                            preview_generated_at=timezone.now(),
                            preview_error="",
                            preview_attempts=0,
                            preview_last_attempt_at=timezone.now(),
                        )
                        generated += 1
                        succeeded = True
                        self.stdout.write(self.style.SUCCESS(f"[{song.pk}] generated"))
                        break
                    except Exception as exc:
                        final_error = str(exc)[:2000]
                        if local_attempt < attempts_per_run:
                            delay = min(2 ** local_attempt, 10)
                            self.stderr.write(
                                self.style.WARNING(
                                    f"[{song.pk}] attempt {local_attempt}/{attempts_per_run} failed; retrying in {delay}s"
                                )
                            )
                            time.sleep(delay)
                    finally:
                        if preview is not None:
                            preview.close()

                if not succeeded:
                    failed += 1
                    message = final_error or "Unknown preview generation error"
                    Song.objects.filter(pk=song.pk).update(
                        preview_error=message,
                        preview_attempts=F("preview_attempts") + 1,
                        preview_last_attempt_at=timezone.now(),
                    )
                    self.stderr.write(self.style.ERROR(f"[{song.pk}] failed: {message}"))

            self.stdout.write(
                self.style.SUCCESS(f"Preview generation complete: generated={generated}, failed={failed}")
            )
