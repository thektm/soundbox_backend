"""Add ArtistAuth fields used by the bilingual onboarding flow without migrations."""

from django.core.management.base import BaseCommand
from django.db import connection

from api.models import ArtistAuth


class Command(BaseCommand):
    help = "Safely add missing bilingual/profile columns to api_artistauth."

    FIELD_NAMES = (
        "first_name_en",
        "last_name_en",
        "stage_name_en",
        "city_en",
        "address_en",
        "biography_en",
        "profile_image",
    )

    def handle(self, *args, **options):
        table = ArtistAuth._meta.db_table
        if table not in connection.introspection.table_names():
            self.stdout.write(self.style.WARNING(f"Skipped missing table: {table}"))
            return

        with connection.cursor() as cursor:
            columns = {
                column.name
                for column in connection.introspection.get_table_description(cursor, table)
            }

        missing = [
            ArtistAuth._meta.get_field(name)
            for name in self.FIELD_NAMES
            if ArtistAuth._meta.get_field(name).column not in columns
        ]
        if not missing:
            self.stdout.write(self.style.SUCCESS("Artist-auth schema is up to date."))
            return

        with connection.schema_editor() as editor:
            for field in missing:
                editor.add_field(ArtistAuth, field)
                self.stdout.write(self.style.SUCCESS(f"Added {table}.{field.column}"))
