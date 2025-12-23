from django.core.management.base import BaseCommand
from api.models import Song, Mood

class Command(BaseCommand):
    help = 'Update all songs to use Farsi mood equivalents instead of English ones'

    def handle(self, *args, **options):
        # Mapping from English mood names to Farsi equivalents
        # NOTE: Update this dictionary with the correct mappings
        mood_mapping = {
            'Happy': 'خوشحال',  # Example: Replace with actual Farsi names
            'Sad': 'غمگین',
            'Energetic': 'پرانرژی',
            'Calm': 'آرام',
            'Romantic': 'عاشقانه',
            # Add more mappings as needed
        }

        updated_songs = 0
        total_replacements = 0

        for song in Song.objects.prefetch_related('moods'):
            original_moods = list(song.moods.all())
            new_moods = []

            for mood in original_moods:
                if mood.name in mood_mapping:
                    # Find the Farsi equivalent
                    farsi_name = mood_mapping[mood.name]
                    try:
                        farsi_mood = Mood.objects.get(name=farsi_name)
                        new_moods.append(farsi_mood)
                        total_replacements += 1
                        self.stdout.write(f'Replacing {mood.name} with {farsi_name} for song "{song.title}"')
                    except Mood.DoesNotExist:
                        self.stdout.write(self.style.WARNING(f'Farsi mood "{farsi_name}" not found for English "{mood.name}"'))
                        new_moods.append(mood)  # Keep original if Farsi not found
                else:
                    new_moods.append(mood)  # Keep if not in mapping

            # Update the many-to-many relationship
            if set(original_moods) != set(new_moods):
                song.moods.set(new_moods)
                updated_songs += 1

        self.stdout.write(self.style.SUCCESS(f'Updated {updated_songs} songs, replaced {total_replacements} mood associations'))