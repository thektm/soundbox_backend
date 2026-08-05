from django.core.management.base import BaseCommand
from django.db import connection

from api.models import PlayConfiguration


class Command(BaseCommand):
    help = 'Ensure the global play configuration contains the minimum payout amount column.'

    def handle(self, *args, **options):
        table = PlayConfiguration._meta.db_table
        field = PlayConfiguration._meta.get_field('minimum_payout_amount')
        column = field.column

        if table not in connection.introspection.table_names():
            self.stdout.write(f'{table} does not exist yet; skipping payout minimum schema check.')
            return

        if connection.vendor == 'postgresql':
            self._ensure_postgresql_column(table, column)
            return

        if self._column_exists(table, column):
            self.stdout.write('Minimum payout amount column already exists.')
            return

        with connection.schema_editor() as schema_editor:
            schema_editor.add_field(PlayConfiguration, field)
        self.stdout.write(self.style.SUCCESS('Added the minimum payout amount column.'))

    def _column_exists(self, table, column):
        with connection.cursor() as cursor:
            description = connection.introspection.get_table_description(cursor, table)
        return any(item.name == column for item in description)

    def _ensure_postgresql_column(self, table, column):
        lock_id = 728_421_908
        qn = connection.ops.quote_name
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_lock(%s)', [lock_id])
            try:
                if self._column_exists(table, column):
                    self.stdout.write('Minimum payout amount column already exists.')
                    return
                cursor.execute(
                    f'ALTER TABLE {qn(table)} ADD COLUMN {qn(column)} '
                    'numeric(15,2) NOT NULL DEFAULT 0.01'
                )
                self.stdout.write(self.style.SUCCESS('Added the minimum payout amount column.'))
            finally:
                cursor.execute('SELECT pg_advisory_unlock(%s)', [lock_id])
