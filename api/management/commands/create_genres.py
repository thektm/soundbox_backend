from django.core.management.base import BaseCommand
from api.models import Genre, SubGenre


class Command(BaseCommand):
    help = 'Create initial music genres and sub-genres'

    def handle(self, *args, **options):
        # Define genres with Persian names and English slugs
        genres_data = [
            {'name': 'پاپ', 'name_en': 'Pop', 'slug': 'pop'},
            {'name': 'راک', 'name_en': 'Rock', 'slug': 'rock'},
            {'name': 'سنتی', 'name_en': 'Traditional', 'slug': 'traditional'},
            {'name': 'رپ', 'name_en': 'Rap', 'slug': 'rap'},
            {'name': 'الکترونیک', 'name_en': 'Electronic', 'slug': 'electronic'},
            {'name': 'جز', 'name_en': 'Jazz', 'slug': 'jazz'},
            {'name': 'بلوز', 'name_en': 'Blues', 'slug': 'blues'},
            {'name': 'متال', 'name_en': 'Metal', 'slug': 'metal'},
            {'name': 'کلاسیک', 'name_en': 'Classical', 'slug': 'classical'},
            {'name': 'فولک', 'name_en': 'Folk', 'slug': 'folk'},
        ]

        # Define sub-genres for each genre
        sub_genres_data = {
            'pop': [
                {'name': 'پاپ ایرانی', 'name_en': 'Persian Pop', 'slug': 'persian-pop'},
                {'name': 'پاپ غربی', 'name_en': 'Western Pop', 'slug': 'western-pop'},
                {'name': 'سینث پاپ', 'name_en': 'Synth-pop', 'slug': 'synth-pop'},
                {'name': 'پاپ راک', 'name_en': 'Pop Rock', 'slug': 'pop-rock'},
            ],
            'rock': [
                {'name': 'راک کلاسیک', 'name_en': 'Classic Rock', 'slug': 'classic-rock'},
                {'name': 'راک آلترناتیو', 'name_en': 'Alternative Rock', 'slug': 'alternative-rock'},
                {'name': 'پانک راک', 'name_en': 'Punk Rock', 'slug': 'punk-rock'},
                {'name': 'هارد راک', 'name_en': 'Hard Rock', 'slug': 'hard-rock'},
            ],
            'traditional': [
                {'name': 'سنتی ایرانی', 'name_en': 'Persian Traditional', 'slug': 'persian-traditional'},
                {'name': 'مقام', 'name_en': 'Maqam', 'slug': 'maqam'},
                {'name': 'آواز', 'name_en': 'Avaz', 'slug': 'avaz'},
                {'name': 'سنتی کردی', 'name_en': 'Kurdish Traditional', 'slug': 'kurdish-traditional'},
            ],
            'rap': [
                {'name': 'رپ ایرانی', 'name_en': 'Persian Rap', 'slug': 'persian-rap'},
                {'name': 'ترپ', 'name_en': 'Trap', 'slug': 'trap'},
                {'name': 'هیپ هاپ', 'name_en': 'Hip-hop', 'slug': 'hip-hop'},
                {'name': 'رپ underground', 'name_en': 'Underground Rap', 'slug': 'underground-rap'},
            ],
            'electronic': [
                {'name': 'هاوس', 'name_en': 'House', 'slug': 'house'},
                {'name': 'ترنس', 'name_en': 'Trance', 'slug': 'trance'},
                {'name': 'تکنو', 'name_en': 'Techno', 'slug': 'techno'},
                {'name': 'EDM', 'name_en': 'EDM', 'slug': 'edm'},
            ],
            'jazz': [
                {'name': 'جز کلاسیک', 'name_en': 'Classic Jazz', 'slug': 'classic-jazz'},
                {'name': 'بیباپ', 'name_en': 'Bebop', 'slug': 'bebop'},
                {'name': 'جز مدرن', 'name_en': 'Modern Jazz', 'slug': 'modern-jazz'},
                {'name': 'جز فیوژن', 'name_en': 'Jazz Fusion', 'slug': 'jazz-fusion'},
            ],
            'blues': [
                {'name': 'بلوز کلاسیک', 'name_en': 'Classic Blues', 'slug': 'classic-blues'},
                {'name': 'دلتا بلوز', 'name_en': 'Delta Blues', 'slug': 'delta-blues'},
                {'name': 'الکتریک بلوز', 'name_en': 'Electric Blues', 'slug': 'electric-blues'},
                {'name': 'بلوز راک', 'name_en': 'Blues Rock', 'slug': 'blues-rock'},
            ],
            'metal': [
                {'name': 'هوی متال', 'name_en': 'Heavy Metal', 'slug': 'heavy-metal'},
                {'name': 'بلک متال', 'name_en': 'Black Metal', 'slug': 'black-metal'},
                {'name': 'دث متال', 'name_en': 'Death Metal', 'slug': 'death-metal'},
                {'name': 'متال‌کور', 'name_en': 'Metalcore', 'slug': 'metalcore'},
            ],
            'classical': [
                {'name': 'اپرا', 'name_en': 'Opera', 'slug': 'opera'},
                {'name': 'سمفونی', 'name_en': 'Symphony', 'slug': 'symphony'},
                {'name': 'کنسرتو', 'name_en': 'Concerto', 'slug': 'concerto'},
                {'name': 'سونات', 'name_en': 'Sonata', 'slug': 'sonata'},
            ],
            'folk': [
                {'name': 'فولک ایرانی', 'name_en': 'Persian Folk', 'slug': 'persian-folk'},
                {'name': 'فولک آمریکایی', 'name_en': 'American Folk', 'slug': 'american-folk'},
                {'name': 'فولک اروپایی', 'name_en': 'European Folk', 'slug': 'european-folk'},
                {'name': 'فولک مدرن', 'name_en': 'Modern Folk', 'slug': 'modern-folk'},
            ],
        }

        # Create genres
        created_genres = 0
        created_sub_genres = 0

        for genre_data in genres_data:
            genre, created = Genre.objects.get_or_create(
                slug=genre_data['slug'],
                defaults={'name': genre_data['name'], 'name_en': genre_data['name_en']}
            )
            if created:
                created_genres += 1
                self.stdout.write(
                    self.style.SUCCESS(f'Created genre: {genre.name} ({genre.slug})')
                )
            else:
                # Update name if it changed
                if genre.name != genre_data['name'] or genre.name_en != genre_data['name_en']:
                    genre.name = genre_data['name']
                    genre.name_en = genre_data['name_en']
                    genre.save(update_fields=['name', 'name_en'])
                    self.stdout.write(
                        self.style.WARNING(f'Updated genre: {genre.name} ({genre.slug})')
                    )

            # Create sub-genres for this genre
            if genre.slug in sub_genres_data:
                for sub_genre_data in sub_genres_data[genre.slug]:
                    sub_genre, sub_created = SubGenre.objects.get_or_create(
                        slug=sub_genre_data['slug'],
                        defaults={
                            'name': sub_genre_data['name'],
                            'name_en': sub_genre_data['name_en'],
                            'parent_genre': genre
                        }
                    )
                    if sub_created:
                        created_sub_genres += 1
                        self.stdout.write(
                            self.style.SUCCESS(f'  Created sub-genre: {sub_genre.name} ({sub_genre.slug})')
                        )
                    else:
                        # Update name if it changed
                        if (sub_genre.name != sub_genre_data['name'] or
                                sub_genre.name_en != sub_genre_data['name_en'] or
                                sub_genre.parent_genre_id != genre.id):
                            sub_genre.name = sub_genre_data['name']
                            sub_genre.name_en = sub_genre_data['name_en']
                            sub_genre.parent_genre = genre
                            sub_genre.save(update_fields=['name', 'name_en', 'parent_genre'])
                            self.stdout.write(
                                self.style.WARNING(f'  Updated sub-genre: {sub_genre.name} ({sub_genre.slug})')
                            )

        self.stdout.write(
            self.style.SUCCESS(
                f'\nSuccessfully processed {len(genres_data)} genres and {sum(len(subs) for subs in sub_genres_data.values())} sub-genres.'
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                f'Created: {created_genres} genres, {created_sub_genres} sub-genres'
            )
        )