"""Install bilingual columns safely on databases created without migrations.

The supplied project keeps ``api/migrations`` empty and deploys schema fixes as
idempotent management commands. This command follows that existing convention:
it discovers every ``*_en`` model field, adds only missing columns, and then
backfills English values for server-owned generated labels where a deterministic
translation exists.
"""

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from api.localization import TAXONOMY_EN_BY_MODEL, translate_generated_text


class Command(BaseCommand):
    help = "Safely add all bilingual *_en columns and backfill generated labels."

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-backfill",
            action="store_true",
            help="Add missing columns without backfilling generated English labels.",
        )

    def handle(self, *args, **options):
        api_config = apps.get_app_config("api")
        existing_tables = set(connection.introspection.table_names())
        added = []
        skipped_tables = []

        for model in api_config.get_models():
            english_fields = [
                field
                for field in model._meta.local_fields
                if field.name.endswith("_en")
            ]
            if not english_fields:
                continue

            table = model._meta.db_table
            if table not in existing_tables:
                skipped_tables.append(table)
                continue

            with connection.cursor() as cursor:
                columns = {
                    column.name
                    for column in connection.introspection.get_table_description(cursor, table)
                }

            missing = [field for field in english_fields if field.column not in columns]
            if not missing:
                continue

            with connection.schema_editor() as schema_editor:
                for field in missing:
                    schema_editor.add_field(model, field)
                    added.append(f"{table}.{field.column}")

        if added:
            self.stdout.write(self.style.SUCCESS("Added bilingual columns:"))
            for name in added:
                self.stdout.write(f"  - {name}")
        else:
            self.stdout.write(self.style.SUCCESS("All bilingual columns already exist."))

        if skipped_tables:
            self.stdout.write(
                self.style.WARNING(
                    "Skipped models whose tables do not exist yet: "
                    + ", ".join(sorted(set(skipped_tables)))
                )
            )

        if not options["skip_backfill"]:
            updated = self._backfill_generated_labels(api_config, existing_tables)
            self.stdout.write(
                self.style.SUCCESS(f"Backfilled {updated} generated English values.")
            )

    @staticmethod
    def _backfill_generated_labels(api_config, existing_tables):
        updated = 0
        with transaction.atomic():
            for model in api_config.get_models():
                if model._meta.db_table not in existing_tables:
                    continue
                model_fields = {field.name: field for field in model._meta.local_fields}
                for english_name, english_field in model_fields.items():
                    if not english_name.endswith("_en"):
                        continue
                    base_name = english_name[:-3]
                    base_field = model_fields.get(base_name)
                    if base_field is None:
                        continue
                    # Generated labels are textual. JSON lists (credits metadata,
                    # producers, etc.) must be translated explicitly by admins.
                    if base_field.get_internal_type() not in {"CharField", "TextField"}:
                        continue

                    only_fields = ["pk", base_name, english_name]
                    if model.__name__ == "RecommendedPlaylist" and "unique_id" in model_fields:
                        only_fields.append("unique_id")

                    for obj in model.objects.only(*only_fields).iterator(chunk_size=500):
                        current = getattr(obj, english_name, None)
                        source = getattr(obj, base_name, None)
                        if not isinstance(source, str) or not source.strip():
                            continue

                        translated = translate_generated_text(source)
                        deterministic = translated and translated != source

                        # Taxonomy labels are server-owned. Known values are
                        # always repaired, even when an older build stored a
                        # romanized/Finglish value in *_en.
                        if model.__name__ in {"Genre", "Mood", "Tag", "SubGenre"}:
                            translated = TAXONOMY_EN_BY_MODEL.get(model.__name__, {}).get(
                                source.strip(), translated
                            )
                            deterministic = translated and translated != source

                        # RecommendedPlaylist is generated by the server. Repair
                        # deterministic templates and smart recommendation rows;
                        # never touch normal user-authored Playlist records.
                        is_smart_recommendation = (
                            model.__name__ == "RecommendedPlaylist"
                            and str(getattr(obj, "unique_id", "")).startswith("smart_rec_")
                        )
                        should_write = (
                            deterministic
                            and (current in (None, "") or model.__name__ in {"Genre", "Mood", "Tag", "SubGenre"} or is_smart_recommendation)
                        )
                        if should_write and current != translated:
                            model.objects.filter(pk=obj.pk).update(**{english_name: translated})
                            updated += 1
        return updated
