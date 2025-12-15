from django.core.management.base import BaseCommand
from django.core.management import call_command


class Command(BaseCommand):
    help = 'Create all initial music data (genres, moods, tags)'

    def handle(self, *args, **options):
        self.stdout.write(
            self.style.SUCCESS('Starting to create initial music data...\n')
        )

        # Create genres and sub-genres
        self.stdout.write('Creating genres and sub-genres...')
        call_command('create_genres', verbosity=1)

        self.stdout.write('\n' + '='*50 + '\n')

        # Create moods
        self.stdout.write('Creating moods...')
        call_command('create_moods', verbosity=1)

        self.stdout.write('\n' + '='*50 + '\n')

        # Create tags
        self.stdout.write('Creating tags...')
        call_command('create_tags', verbosity=1)

        self.stdout.write('\n' + '='*50)
        self.stdout.write(
            self.style.SUCCESS('All initial music data created successfully!')
        )
        self.stdout.write(
            self.style.SUCCESS('You can now use these in your song uploads.')
        )