import os

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import Genre, SubGenre


MARKER_NAME = '.initial_genres_repaired_v1'

GENRE_DATA = {
    'pop': ('پاپ', 'Pop'),
    'rock': ('راک', 'Rock'),
    'traditional': ('سنتی', 'Traditional'),
    'rap': ('رپ', 'Rap'),
    'electronic': ('الکترونیک', 'Electronic'),
    'jazz': ('جز', 'Jazz'),
    'blues': ('بلوز', 'Blues'),
    'metal': ('متال', 'Metal'),
    'classical': ('کلاسیک', 'Classical'),
    'folk': ('فولک', 'Folk'),
    'r-and-b': ('آر اند بی', 'R&B'),
    'fusion': ('تلفیقی', 'Fusion'),
    'hip-hop': ('هیپ هاپ', 'Hip-hop'),
    'soundtrack': ('موسیقی متن', 'Soundtrack'),
    'alternative': ('آلترناتیو', 'Alternative'),
    'disco': ('دیسکو', 'Disco'),
    'funk': ('فانک', 'Funk'),
    'soul': ('سول', 'Soul'),
}

GENRE_ALIASES = {
    'پاپ': 'pop',
    'راک': 'rock',
    'سنتی': 'traditional',
    'رپ': 'rap',
    'الکترونیک': 'electronic',
    'جز': 'jazz',
    'بلوز': 'blues',
    'متال': 'metal',
    'کلاسیک': 'classical',
    'فولک': 'folk',
    'آر اند بی': 'r-and-b',
    'تلفیقی': 'fusion',
    'هیپ‌هاپ': 'hip-hop',
    'هیپ هاپ': 'hip-hop',
    'موسیقی متن': 'soundtrack',
    'آلترناتیو': 'alternative',
    'دیسکو': 'disco',
    'فانک': 'funk',
    'سول': 'soul',
}

SUBGENRE_DATA = {
    'persian-pop': ('پاپ ایرانی', 'Persian Pop'),
    'western-pop': ('پاپ غربی', 'Western Pop'),
    'synth-pop': ('سینث پاپ', 'Synth-pop'),
    'pop-rock': ('پاپ راک', 'Pop Rock'),
    'classic-rock': ('راک کلاسیک', 'Classic Rock'),
    'alternative-rock': ('راک آلترناتیو', 'Alternative Rock'),
    'punk-rock': ('پانک راک', 'Punk Rock'),
    'hard-rock': ('هارد راک', 'Hard Rock'),
    'persian-traditional': ('سنتی ایرانی', 'Persian Traditional'),
    'maqam': ('مقام', 'Maqam'),
    'avaz': ('آواز', 'Avaz'),
    'kurdish-traditional': ('سنتی کردی', 'Kurdish Traditional'),
    'persian-rap': ('رپ ایرانی', 'Persian Rap'),
    'trap': ('ترپ', 'Trap'),
    'hip-hop': ('هیپ هاپ', 'Hip-hop'),
    'underground-rap': ('رپ underground', 'Underground Rap'),
    'house': ('هاوس', 'House'),
    'trance': ('ترنس', 'Trance'),
    'techno': ('تکنو', 'Techno'),
    'edm': ('EDM', 'EDM'),
    'classic-jazz': ('جز کلاسیک', 'Classic Jazz'),
    'bebop': ('بیباپ', 'Bebop'),
    'modern-jazz': ('جز مدرن', 'Modern Jazz'),
    'jazz-fusion': ('جز فیوژن', 'Jazz Fusion'),
    'classic-blues': ('بلوز کلاسیک', 'Classic Blues'),
    'delta-blues': ('دلتا بلوز', 'Delta Blues'),
    'electric-blues': ('الکتریک بلوز', 'Electric Blues'),
    'blues-rock': ('بلوز راک', 'Blues Rock'),
    'heavy-metal': ('هوی متال', 'Heavy Metal'),
    'black-metal': ('بلک متال', 'Black Metal'),
    'death-metal': ('دث متال', 'Death Metal'),
    'metalcore': ('متال‌کور', 'Metalcore'),
    'opera': ('اپرا', 'Opera'),
    'symphony': ('سمفونی', 'Symphony'),
    'concerto': ('کنسرتو', 'Concerto'),
    'sonata': ('سونات', 'Sonata'),
    'persian-folk': ('فولک ایرانی', 'Persian Folk'),
    'american-folk': ('فولک آمریکایی', 'American Folk'),
    'european-folk': ('فولک اروپایی', 'European Folk'),
    'modern-folk': ('فولک مدرن', 'Modern Folk'),
}


def _ascii_name(value):
    return bool(value) and all(ord(char) < 128 for char in value)


class Command(BaseCommand):
    help = 'Repair the initial genre and sub-genre records once, then persist a completion marker.'

    def handle(self, *args, **options):
        marker_path = os.path.join(settings.MEDIA_ROOT, MARKER_NAME)
        if os.path.exists(marker_path):
            self.stdout.write(self.style.NOTICE('Initial genre repair already completed; skipping.'))
            return

        genre_updates = 0
        subgenre_updates = 0
        with transaction.atomic():
            for genre in Genre.objects.all():
                key = genre.slug.lower().strip()
                key = GENRE_ALIASES.get(genre.name.strip(), key)
                if key in GENRE_DATA:
                    name, name_en = GENRE_DATA[key]
                    values = {'name': name, 'name_en': name_en, 'slug': key}
                elif not genre.name_en and _ascii_name(genre.name):
                    values = {'name_en': genre.name}
                else:
                    continue

                changed = any(getattr(genre, field) != value for field, value in values.items())
                if changed:
                    for field, value in values.items():
                        setattr(genre, field, value)
                    genre.save(update_fields=list(values))
                    genre_updates += 1

            for subgenre in SubGenre.objects.all():
                key = subgenre.slug.lower().strip()
                if key in SUBGENRE_DATA:
                    name, name_en = SUBGENRE_DATA[key]
                    values = {'name': name, 'name_en': name_en}
                elif not subgenre.name_en and _ascii_name(subgenre.name):
                    values = {'name_en': subgenre.name}
                else:
                    continue

                changed = any(getattr(subgenre, field) != value for field, value in values.items())
                if changed:
                    for field, value in values.items():
                        setattr(subgenre, field, value)
                    subgenre.save(update_fields=list(values))
                    subgenre_updates += 1

        os.makedirs(settings.MEDIA_ROOT, exist_ok=True)
        with open(marker_path, 'x', encoding='ascii') as marker:
            marker.write('completed\n')

        self.stdout.write(self.style.SUCCESS(
            f'Repaired {genre_updates} genres and {subgenre_updates} sub-genres. '
            f'Marker created at {marker_path}.'
        ))
