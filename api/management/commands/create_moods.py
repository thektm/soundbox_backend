from django.core.management.base import BaseCommand
from api.models import Mood


class Command(BaseCommand):
    help = 'Create initial music moods'

    def handle(self, *args, **options):
        # Define moods with Persian names and English slugs
        moods_data = [
            {'name': 'شاد', 'name_en': 'Happy', 'slug': 'happy'},
            {'name': 'غمگین', 'name_en': 'Sad', 'slug': 'sad'},
            {'name': 'عاشقانه', 'name_en': 'Romantic', 'slug': 'romantic'},
            {'name': 'انرژیک', 'name_en': 'Energetic', 'slug': 'energetic'},
            {'name': 'آرام', 'name_en': 'Calm', 'slug': 'calm'},
            {'name': 'هیجان‌انگیز', 'name_en': 'Exciting', 'slug': 'exciting'},
            {'name': 'مذهبی', 'name_en': 'Spiritual', 'slug': 'spiritual'},
            {'name': 'پارتی', 'name_en': 'Party', 'slug': 'party'},
            {'name': 'تمرکز', 'name_en': 'Focus', 'slug': 'focus'},
            {'name': 'خواب', 'name_en': 'Sleep', 'slug': 'sleep'},
            {'name': 'ورزشی', 'name_en': 'Workout', 'slug': 'workout'},
            {'name': 'موتورسواری', 'name_en': 'Driving', 'slug': 'driving'},
            {'name': 'نوستالژیک', 'name_en': 'Nostalgic', 'slug': 'nostalgic'},
            {'name': 'الهام‌بخش', 'name_en': 'Inspirational', 'slug': 'inspirational'},
            {'name': 'رقص', 'name_en': 'Dance', 'slug': 'dance'},
        ]

        created_count = 0
        updated_count = 0

        for mood_data in moods_data:
            mood, created = Mood.objects.get_or_create(
                slug=mood_data['slug'],
                defaults={'name': mood_data['name'], 'name_en': mood_data['name_en']}
            )
            if created:
                created_count += 1
                self.stdout.write(
                    self.style.SUCCESS(f'Created mood: {mood.name} ({mood.slug})')
                )
            else:
                # Update name if it changed
                if mood.name != mood_data['name'] or mood.name_en != mood_data['name_en']:
                    mood.name = mood_data['name']
                    mood.name_en = mood_data['name_en']
                    mood.save(update_fields=['name', 'name_en'])
                    updated_count += 1
                    self.stdout.write(
                        self.style.WARNING(f'Updated mood: {mood.name} ({mood.slug})')
                    )

        self.stdout.write(
            self.style.SUCCESS(
                f'\nSuccessfully processed {len(moods_data)} moods.'
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                f'Created: {created_count} moods, Updated: {updated_count} moods'
            )
        )