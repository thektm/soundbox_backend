from django.core.management.base import BaseCommand
from django.db import connection

from api.models import (
    ArtistRelease,
    ArtistReleaseStatusHistory,
    ArtistReleaseTrack,
    ReleaseContributor,
    Song,
)


class Command(BaseCommand):
    help = 'Create the additive artist-release workflow tables when migrations are unavailable.'

    def handle(self, *args, **options):
        # Both the web process and the optional scheduler container can start at
        # the same time. A PostgreSQL advisory lock makes this idempotent schema
        # bootstrap safe under concurrent container startup.
        advisory_lock_id = 728_421_906
        advisory_locked = False
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_lock(%s)', [advisory_lock_id])
            advisory_locked = True

        try:
            models = [ReleaseContributor, ArtistRelease, ArtistReleaseTrack, ArtistReleaseStatusHistory]
            existing = set(connection.introspection.table_names())
            created = []
            with connection.schema_editor() as editor:
                with connection.cursor() as cursor:
                    song_columns = {
                        column.name
                        for column in connection.introspection.get_table_description(cursor, Song._meta.db_table)
                    }
                for field_name in ('album_disc_number', 'album_track_number'):
                    field = Song._meta.get_field(field_name)
                    if field.column not in song_columns:
                        editor.add_field(Song, field)
                        song_columns.add(field.column)
                        created.append(f'{Song._meta.db_table}.{field.column}')
                for model in models:
                    if model._meta.db_table in existing:
                        continue
                    editor.create_model(model)
                    existing.add(model._meta.db_table)
                    created.append(model._meta.db_table)
            if created:
                self.stdout.write(self.style.SUCCESS(f"Created release workflow schema: {', '.join(created)}"))
            else:
                self.stdout.write('Artist release workflow schema is already present.')
        finally:
            if advisory_locked:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_advisory_unlock(%s)', [advisory_lock_id])
