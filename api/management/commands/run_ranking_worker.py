import logging
import os
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from api.recommendation_runtime import get_redis_client
from api.trending import trending_song_ids

logger = logging.getLogger(__name__)
_LOCK_KEY = 'sedabox:ranking-worker:trending-refresh:v1'


class Command(BaseCommand):
    help = 'Precompute expensive global ranking caches outside request workers.'

    def handle(self, *args, **options):
        # Background ranking should yield CPU to HTTP/WebSocket workers under load.
        try:
            os.nice(5)
        except (AttributeError, OSError):
            pass
        interval = max(30, int(getattr(settings, 'TRENDING_REFRESH_INTERVAL', 90)))
        self.stdout.write('ranking worker ready')
        while True:
            started = time.monotonic()
            client = get_redis_client()
            token = f'{os.getpid()}:{time.time_ns()}'
            claimed = True
            try:
                if client is not None:
                    claimed = bool(client.set(_LOCK_KEY, token, nx=True, ex=max(120, interval * 2)))
                if claimed:
                    close_old_connections()
                    trending_song_ids(require_preview=False, force=True)
                    trending_song_ids(require_preview=True, force=True)
            except Exception:
                logger.exception('Trending ranking refresh failed')
            finally:
                close_old_connections()
                if claimed and client is not None:
                    try:
                        if client.get(_LOCK_KEY) == token:
                            client.delete(_LOCK_KEY)
                    except Exception:
                        pass
            elapsed = time.monotonic() - started
            time.sleep(max(5, interval - elapsed))
