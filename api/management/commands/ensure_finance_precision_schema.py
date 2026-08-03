from django.core.management.base import BaseCommand
from django.db import connection

from api.models import PlayCount


class Command(BaseCommand):
    help = 'Ensure play royalties keep the full eight-decimal configured precision.'

    def handle(self, *args, **options):
        table = PlayCount._meta.db_table
        column = PlayCount._meta.get_field('pay').column
        if table not in connection.introspection.table_names():
            self.stdout.write(f'{table} does not exist yet; skipping finance precision check.')
            return

        if connection.vendor != 'postgresql':
            self.stdout.write('Finance precision bootstrap currently targets PostgreSQL; no change required here.')
            return

        lock_id = 728_421_907
        qn = connection.ops.quote_name
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_lock(%s)', [lock_id])
            try:
                cursor.execute(
                    '''
                    SELECT numeric_precision, numeric_scale
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = %s
                      AND column_name = %s
                    ''',
                    [table, column],
                )
                row = cursor.fetchone()
                if row and (row[0] or 0) >= 15 and (row[1] or 0) >= 8:
                    self.stdout.write('Play royalty precision is already numeric(15,8).')
                    return

                cursor.execute(
                    f'ALTER TABLE {qn(table)} ALTER COLUMN {qn(column)} '
                    f'TYPE numeric(15,8) USING {qn(column)}::numeric(15,8)'
                )
                self.stdout.write(self.style.SUCCESS('Updated play royalty precision to numeric(15,8).'))
            finally:
                cursor.execute('SELECT pg_advisory_unlock(%s)', [lock_id])
