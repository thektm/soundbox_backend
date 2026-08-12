import os
import uuid

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import ArtistAuth
from api.utils import r2_object_key, upload_file_to_r2


class Command(BaseCommand):
    help = 'Move legacy ArtistAuth profile/national-ID images from local media storage to private R2.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=0, help='Maximum number of ArtistAuth rows to inspect (0 = all).')

    def handle(self, *args, **options):
        limit = max(0, int(options.get('limit') or 0))
        qs = ArtistAuth.objects.select_related('user').order_by('pk')
        if limit:
            qs = qs[:limit]

        inspected = migrated = already_r2 = missing = failed = 0
        for auth in qs.iterator(chunk_size=100):
            inspected += 1
            changed = []
            for field_name, folder, kind in (
                ('profile_image', 'artist-auth/profiles', 'profile'),
                ('national_id_image', 'artist-auth/national-ids', 'national-id'),
            ):
                field = getattr(auth, field_name, None)
                raw = str(getattr(field, 'name', '') or '').strip()
                if not raw:
                    continue
                if r2_object_key(raw, allow_key=False):
                    already_r2 += 1
                    continue
                try:
                    extension = os.path.splitext(raw)[1].lower()
                    if extension not in {'.jpg', '.jpeg', '.png'}:
                        extension = '.jpg'
                    user_part = getattr(auth.user, 'unique_id', None) or auth.user_id or 'unknown'
                    filename = f'u{user_part}-{kind}-{uuid.uuid4().hex[:12]}{extension}'
                    field.open('rb')
                    url, _ = upload_file_to_r2(
                        field,
                        folder=folder,
                        custom_filename=filename,
                        check_existing=False,
                    )
                    setattr(auth, field_name, url)
                    changed.append(field_name)
                except FileNotFoundError:
                    missing += 1
                    self.stderr.write(f'auth={auth.pk} field={field_name}: local file is missing: {raw}')
                except Exception as exc:
                    failed += 1
                    self.stderr.write(f'auth={auth.pk} field={field_name}: migration failed: {exc}')

            if changed:
                with transaction.atomic():
                    auth.save(update_fields=changed)
                migrated += len(changed)
                self.stdout.write(f'auth={auth.pk}: migrated {", ".join(changed)}')

        self.stdout.write(self.style.SUCCESS(
            f'Done. rows={inspected} migrated_files={migrated} already_r2={already_r2} '
            f'missing_local={missing} failed={failed}'
        ))
