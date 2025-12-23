from django.core.management.base import BaseCommand
from api.models import Song, Mood

class Command(BaseCommand):
    help = 'List all unique moods associated with songs in the database'

    def handle(self, *args, **options):
        # Get all unique moods from songs
        moods = Mood.objects.filter(song__isnull=False).distinct().order_by('name')
        
        self.stdout.write(self.style.SUCCESS(f'Found {moods.count()} unique moods in songs:'))
        
        for mood in moods:
            song_count = Song.objects.filter(moods=mood).count()
            self.stdout.write(f'- {mood.name} (ID: {mood.id}, Slug: {mood.slug}) - Used in {song_count} songs')