from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Safely add guest-preview columns to an existing PostgreSQL api_song table."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            self.stdout.write(self.style.WARNING("Preview schema audit skipped: PostgreSQL is required."))
            return

        if "api_song" not in connection.introspection.table_names():
            self.stdout.write(self.style.WARNING("Preview schema audit skipped: api_song does not exist yet."))
            return

        statements = [
            "ALTER TABLE api_song ADD COLUMN IF NOT EXISTS preview_audio_url varchar(500)",
            "ALTER TABLE api_song ADD COLUMN IF NOT EXISTS preview_generated_at timestamptz",
            "ALTER TABLE api_song ADD COLUMN IF NOT EXISTS preview_error text NOT NULL DEFAULT ''",
            "ALTER TABLE api_song ADD COLUMN IF NOT EXISTS preview_attempts smallint NOT NULL DEFAULT 0",
            "ALTER TABLE api_song ADD COLUMN IF NOT EXISTS preview_last_attempt_at timestamptz",
            """DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'api_song_preview_attempts_nonnegative'
                ) THEN
                    ALTER TABLE api_song ADD CONSTRAINT api_song_preview_attempts_nonnegative
                    CHECK (preview_attempts >= 0) NOT VALID;
                END IF;
            END $$""",
        ]
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)

        self.stdout.write(self.style.SUCCESS("Guest preview schema is ready."))
