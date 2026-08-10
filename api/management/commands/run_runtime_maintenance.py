import logging
import os
import time

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from api.runtime_maintenance import cleanup_runtime_state

logger = logging.getLogger(__name__)


def enabled(name, default='1'):
    return os.getenv(name, default).lower() in {'1', 'true', 'yes', 'on'}


class Command(BaseCommand):
    help = 'Run safe bounded runtime cleanup and preview backfill outside web workers.'

    def handle(self, *args, **options):
        try:
            os.nice(5)
        except (AttributeError, OSError):
            pass

        cleanup_interval = max(300, int(getattr(settings, 'RUNTIME_MAINTENANCE_INTERVAL', 900)))
        preview_interval = max(300, int(os.getenv('PREVIEW_MAINTENANCE_INTERVAL', '300')))
        preview_delay = max(30, int(os.getenv('PREVIEW_STARTUP_DELAY_SECONDS', '60')))
        preview_enabled = enabled('GENERATE_PREVIEWS_ON_STARTUP', '1')
        next_cleanup = 0.0
        next_preview = time.monotonic() + preview_delay
        startup = True

        self.stdout.write('runtime maintenance worker ready')
        while True:
            now = time.monotonic()
            try:
                if now >= next_cleanup:
                    cleanup_runtime_state(startup=startup)
                    startup = False
                    next_cleanup = time.monotonic() + cleanup_interval

                if preview_enabled and now >= next_preview:
                    # Small batches + one FFmpeg thread keep media backfill from
                    # contending with request workers on modest hosts.
                    call_command(
                        'generate_missing_previews',
                        limit=max(1, int(os.getenv('PREVIEW_MAINTENANCE_BATCH', '2'))),
                        attempts_per_run=1,
                    )
                    next_preview = time.monotonic() + preview_interval
            except Exception:
                logger.exception('Runtime maintenance cycle failed')
                # Avoid hot-looping on a persistent media/DB problem.
                if next_cleanup <= now:
                    next_cleanup = time.monotonic() + min(cleanup_interval, 60)
                if next_preview <= now:
                    next_preview = time.monotonic() + min(preview_interval, 60)
            finally:
                close_old_connections()

            deadlines = [next_cleanup]
            if preview_enabled:
                deadlines.append(next_preview)
            sleep_for = max(5.0, min(deadlines) - time.monotonic())
            time.sleep(min(sleep_for, 60.0))
