from django.core.management.base import BaseCommand
from django.db import connection

from api.models import SongPromotion, SupportTicket


class Command(BaseCommand):
    help = 'Create additive admin-panel support/promotion tables when migrations are unavailable.'

    def handle(self, *args, **options):
        advisory_lock_id = 728_421_909
        advisory_locked = False
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_lock(%s)', [advisory_lock_id])
            advisory_locked = True

        try:
            existing = set(connection.introspection.table_names())
            created = []
            with connection.schema_editor() as editor:
                for model in (SupportTicket, SongPromotion):
                    if model._meta.db_table in existing:
                        continue
                    editor.create_model(model)
                    existing.add(model._meta.db_table)
                    created.append(model._meta.db_table)
            if created:
                self.stdout.write(self.style.SUCCESS(f"Created admin panel schema: {', '.join(created)}"))
            else:
                self.stdout.write('Admin panel additive schema is already present.')
        finally:
            if advisory_locked:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_advisory_unlock(%s)', [advisory_lock_id])
