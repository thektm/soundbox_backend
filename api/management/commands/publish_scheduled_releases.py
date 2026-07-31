import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from api.release_service import publish_due_releases


class Command(BaseCommand):
    help = 'Publish artist releases whose scheduled release time has arrived.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument('--watch', action='store_true', help='Keep checking instead of exiting after one pass.')
        parser.add_argument('--interval', type=int, default=30, help='Seconds between checks in watch mode (minimum 5).')

    def handle(self, *args, **options):
        limit = max(1, options['limit'])
        watch = bool(options['watch'])
        interval = max(5, int(options['interval']))

        while True:
            close_old_connections()
            try:
                count = publish_due_releases(limit=limit)
                if count:
                    self.stdout.write(self.style.SUCCESS(f'Published {count} scheduled release(s).'))
                elif not watch:
                    self.stdout.write('No scheduled releases were due.')
            except Exception as exc:
                if not watch:
                    raise
                self.stderr.write(self.style.ERROR(f'Scheduled release check failed: {exc}'))
            finally:
                close_old_connections()

            if not watch:
                return
            time.sleep(interval)
