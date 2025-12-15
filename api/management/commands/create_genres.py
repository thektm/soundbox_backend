from django.core.management.base import BaseCommand
from api.models import Genre, SubGenre


class Command(BaseCommand):
    help = 'Create initial music genres and sub-genres'

    def handle(self, *args, **options):
        # Define genres with Persian names and English slugs
        genres_data = [
            {'name': 'پاپ', 'slug': 'pop'},
            {'name': 'راک', 'slug': 'rock'},
            {'name': 'سنتی', 'slug': 'traditional'},
            {'name': 'رپ', 'slug': 'rap'},
            {'name': 'الکترونیک', 'slug': 'electronic'},
            {'name': 'جز', 'slug': 'jazz'},
            {'name': 'بلوز', 'slug': 'blues'},
            {'name': 'متال', 'slug': 'metal'},
            {'name': 'کلاسیک', 'slug': 'classical'},
            {'name': 'فولک', 'slug': 'folk'},
        ]

        # Define sub-genres for each genre
        sub_genres_data = {
            'pop': [
                {'name': 'پاپ ایرانی', 'slug': 'persian-pop'},
                {'name': 'پاپ غربی', 'slug': 'western-pop'},
                {'name': 'سینث پاپ', 'slug': 'synth-pop'},
                {'name': 'پاپ راک', 'slug': 'pop-rock'},
            ],
            'rock': [
                {'name': 'راک کلاسیک', 'slug': 'classic-rock'},
                {'name': 'راک آلترناتیو', 'slug': 'alternative-rock'},
                {'name': 'پانک راک', 'slug': 'punk-rock'},
                {'name': 'هارد راک', 'slug': 'hard-rock'},
            ],
            'traditional': [
                {'name': 'سنتی ایرانی', 'slug': 'persian-traditional'},
                {'name': 'مقام', 'slug': 'maqam'},
                {'name': 'آواز', 'slug': 'avaz'},
                {'name': 'سنتی کردی', 'slug': 'kurdish-traditional'},
            ],
            'rap': [
                {'name': 'رپ ایرانی', 'slug': 'persian-rap'},
                {'name': 'ترپ', 'slug': 'trap'},
                {'name': 'هیپ هاپ', 'slug': 'hip-hop'},
                {'name': 'رپ underground', 'slug': 'underground-rap'},
            ],
            'electronic': [
                {'name': 'هاوس', 'slug': 'house'},
                {'name': 'ترنس', 'slug': 'trance'},
                {'name': 'تکنو', 'slug': 'techno'},
                {'name': 'EDM', 'slug': 'edm'},
            ],
            'jazz': [
                {'name': 'جز کلاسیک', 'slug': 'classic-jazz'},
                {'name': 'بیباپ', 'slug': 'bebop'},
                {'name': 'جز مدرن', 'slug': 'modern-jazz'},
                {'name': 'جز فیوژن', 'slug': 'jazz-fusion'},
            ],
            'blues': [
                {'name': 'بلوز کلاسیک', 'slug': 'classic-blues'},
                {'name': 'دلتا بلوز', 'slug': 'delta-blues'},
                {'name': 'الکتریک بلوز', 'slug': 'electric-blues'},
                {'name': 'بلوز راک', 'slug': 'blues-rock'},
            ],
            'metal': [
                {'name': 'هوی متال', 'slug': 'heavy-metal'},
                {'name': 'بلک متال', 'slug': 'black-metal'},
                {'name': 'دث متال', 'slug': 'death-metal'},
                {'name': 'متال‌کور', 'slug': 'metalcore'},
            ],
            'classical': [
                {'name': 'اپرا', 'slug': 'opera'},
                {'name': 'سمفونی', 'slug': 'symphony'},
                {'name': 'کنسرتو', 'slug': 'concerto'},
                {'name': 'سونات', 'slug': 'sonata'},
            ],
            'folk': [
                {'name': 'فولک ایرانی', 'slug': 'persian-folk'},
                {'name': 'فولک آمریکایی', 'slug': 'american-folk'},
                {'name': 'فولک اروپایی', 'slug': 'european-folk'},
                {'name': 'فولک مدرن', 'slug': 'modern-folk'},
            ],
        }

        # Create genres
        created_genres = 0
        created_sub_genres = 0

        for genre_data in genres_data:
            genre, created = Genre.objects.get_or_create(
                slug=genre_data['slug'],
                defaults={'name': genre_data['name']}
            )
            if created:
                created_genres += 1
                self.stdout.write(
                    self.style.SUCCESS(f'Created genre: {genre.name} ({genre.slug})')
                )
            else:
                # Update name if it changed
                if genre.name != genre_data['name']:
                    genre.name = genre_data['name']
                    genre.save()
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
                        if sub_genre.name != sub_genre_data['name']:
                            sub_genre.name = sub_genre_data['name']
                            sub_genre.parent_genre = genre
                            sub_genre.save()
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