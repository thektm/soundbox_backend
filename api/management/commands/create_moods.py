from django.core.management.base import BaseCommand
from api.models import Mood


class Command(BaseCommand):
    help = 'Create initial music moods'

    def handle(self, *args, **options):
        # Define moods with Persian names and English slugs
        moods_data = [
            {'name': 'شاد', 'slug': 'happy'},
            {'name': 'غمگین', 'slug': 'sad'},
            {'name': 'عاشقانه', 'slug': 'romantic'},
            {'name': 'انرژیک', 'slug': 'energetic'},
            {'name': 'آرام', 'slug': 'calm'},
            {'name': 'هیجان‌انگیز', 'slug': 'exciting'},
            {'name': 'مذهبی', 'slug': 'spiritual'},
            {'name': 'پارتی', 'slug': 'party'},
            {'name': 'تمرکز', 'slug': 'focus'},
            {'name': 'خواب', 'slug': 'sleep'},
            {'name': 'ورزشی', 'slug': 'workout'},
            {'name': 'موتورسواری', 'slug': 'driving'},
            {'name': 'نوستالژیک', 'slug': 'nostalgic'},
            {'name': 'الهام‌بخش', 'slug': 'inspirational'},
            {'name': 'رقص', 'slug': 'dance'},
        ]

        created_count = 0
        updated_count = 0

        for mood_data in moods_data:
            mood, created = Mood.objects.get_or_create(
                slug=mood_data['slug'],
                defaults={'name': mood_data['name']}
            )
            if created:
                created_count += 1
                self.stdout.write(
                    self.style.SUCCESS(f'Created mood: {mood.name} ({mood.slug})')
                )
            else:
                # Update name if it changed
                if mood.name != mood_data['name']:
                    mood.name = mood_data['name']
                    mood.save()
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