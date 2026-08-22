from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.models import Q


class Command(BaseCommand):
    help = 'Add and backfill server-generated unique IDs for playlists.'

    models = ('Playlist', 'UserPlaylist')

    def handle(self, *args, **options):
        api_config = apps.get_app_config('api')
        existing_tables = set(connection.introspection.table_names())
        updated = 0

        for model_name in self.models:
            model = api_config.get_model(model_name)
            table = model._meta.db_table
            if table not in existing_tables:
                continue

            field = model._meta.get_field('unique_id')
            added_nullable_field = None
            with connection.cursor() as cursor:
                columns = {
                    column.name
                    for column in connection.introspection.get_table_description(cursor, table)
                }

            if field.column not in columns:
                nullable_field = field.clone()
                nullable_field.null = True
                nullable_field.unique = False
                nullable_field.default = None
                with connection.schema_editor() as schema_editor:
                    schema_editor.add_field(model, nullable_field)
                added_nullable_field = nullable_field

            rows = model.objects.filter(
                Q(unique_id__isnull=True) | Q(unique_id='')
            ).only('pk', 'unique_id')
            with transaction.atomic():
                for row in rows.iterator():
                    row.save()
                    updated += 1

            with connection.cursor() as cursor:
                columns = {
                    column.name
                    for column in connection.introspection.get_table_description(cursor, table)
                }
            if added_nullable_field is not None:
                with connection.schema_editor() as schema_editor:
                    schema_editor.alter_field(
                        model, added_nullable_field, field, strict=False
                    )

        self.stdout.write(self.style.SUCCESS(
            f'Playlist unique IDs ready; backfilled {updated} rows.'
        ))
