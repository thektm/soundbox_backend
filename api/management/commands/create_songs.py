from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_date
from api.models import Artist, Album, Genre, SubGenre, Mood, Tag, Song, User
import json
import os


def _get_or_create_genre(slug):
    genre, _ = Genre.objects.get_or_create(slug=slug, defaults={'name': slug.replace('-', ' ').title()})
    return genre


def _get_or_create_subgenre(slug, parent_genre=None):
    sub, _ = SubGenre.objects.get_or_create(slug=slug, defaults={'name': slug.replace('-', ' ').title(), 'parent_genre': parent_genre})
    # ensure parent_genre set if provided
    if parent_genre and sub.parent_genre_id != parent_genre.id:
        sub.parent_genre = parent_genre
        sub.save()
    return sub


def _get_or_create_mood(slug):
    mood, _ = Mood.objects.get_or_create(slug=slug, defaults={'name': slug.replace('-', ' ').title()})
    return mood


def _get_or_create_tag(slug):
    tag, _ = Tag.objects.get_or_create(slug=slug, defaults={'name': slug.replace('-', ' ').title()})
    return tag


class Command(BaseCommand):
    help = 'Create initial songs from a JSON seed file (or embedded sample)'

    def add_arguments(self, parser):
        parser.add_argument('--file', type=str, help='Path to JSON seed file', default='deploy/songs_seed.json')

    def handle(self, *args, **options):
        seed_path = options.get('file')

        songs_data = []

        # Try to load external JSON if present
        if seed_path and os.path.exists(seed_path):
            self.stdout.write(self.style.NOTICE(f'Loading songs from {seed_path}'))
            with open(seed_path, 'r', encoding='utf-8') as fh:
                songs_data = json.load(fh)
        else:
            # Fallback sample data (small) to show structure
            self.stdout.write(self.style.WARNING('No seed file found; using embedded sample data.'))
            songs_data = [
                {
                    'title': 'Sample Song 1',
                    'artist': 'Sample Artist',
                    'album': 'Sample Album',
                    'is_single': False,
                    'audio_file': 'https://cdn.example.com/songs/sample-song-1.mp3',
                    'cover_image': 'https://cdn.example.com/covers/sample-song-1.jpg',
                    'original_format': 'mp3',
                    'duration_seconds': 215,
                    'release_date': '2020-01-01',
                    'language': 'fa',
                    'genres': ['pop'],
                    'sub_genres': ['persian-pop'],
                    'moods': ['happy'],
                    'tags': ['popular', 'top'],
                    'featured_artists': [],
                    'producers': [],
                    'composers': [],
                    'lyricists': [],
                    'description': 'A sample song used for seeding the DB',
                    'lyrics': '',
                    'tempo': 100,
                    'energy': 70,
                    'danceability': 60,
                    'valence': 65,
                }
            ]

        created = 0
        updated = 0

        for item in songs_data:
            title = item.get('title')
            artist_name = item.get('artist')
            if not title or not artist_name or not item.get('audio_file'):
                self.stdout.write(self.style.ERROR(f"Skipping entry missing required fields: {item}"))
                continue

            artist, _ = Artist.objects.get_or_create(name=artist_name)

            # album handling
            album_obj = None
            album_title = item.get('album')
            if album_title:
                album_obj, _ = Album.objects.get_or_create(title=album_title, artist=artist)

            # find existing song by title+artist
            song_qs = Song.objects.filter(title=title, artist=artist)
            if song_qs.exists():
                song = song_qs.first()
                is_new = False
            else:
                song = Song(title=title, artist=artist)
                is_new = True

            # set scalar fields
            song.album = album_obj
            song.is_single = item.get('is_single', False)
            song.audio_file = item.get('audio_file')
            song.cover_image = item.get('cover_image', '')
            song.original_format = item.get('original_format', '')
            song.duration_seconds = item.get('duration_seconds')
            rd = item.get('release_date')
            if rd:
                try:
                    song.release_date = parse_date(rd)
                except Exception:
                    song.release_date = None
            song.language = item.get('language', 'fa')
            song.description = item.get('description', '')
            song.lyrics = item.get('lyrics', '')
            song.tempo = item.get('tempo')
            song.energy = item.get('energy')
            song.danceability = item.get('danceability')
            song.valence = item.get('valence')
            song.acousticness = item.get('acousticness')
            song.instrumentalness = item.get('instrumentalness')
            song.live_performed = item.get('live_performed', False)
            song.speechiness = item.get('speechiness')
            song.label = item.get('label', '')
            song.producers = item.get('producers', [])
            song.composers = item.get('composers', [])
            song.lyricists = item.get('lyricists', [])
            song.credits = item.get('credits', '')
            song.featured_artists = item.get('featured_artists', [])
            song.status = item.get('status', Song.STATUS_PUBLISHED)

            # uploader: optional phone number in seed file for linking uploader user
            uploader_phone = item.get('uploader_phone')
            if uploader_phone:
                try:
                    uploader = User.objects.filter(phone_number=str(uploader_phone)).first()
                    if uploader:
                        song.uploader = uploader
                except Exception:
                    pass

            song.save()

            # Many-to-many relations
            # genres
            song.genres.clear()
            for gslug in item.get('genres', []):
                g = _get_or_create_genre(gslug)
                song.genres.add(g)

            # sub-genres
            song.sub_genres.clear()
            for sslug in item.get('sub_genres', []):
                # try to associate parent if matching genre exists
                parent = None
                # naive parent finding: first genre that has same prefix
                for g in song.genres.all():
                    if g.slug and sslug.startswith(g.slug.split('-')[0]):
                        parent = g
                        break
                sub = _get_or_create_subgenre(sslug, parent_genre=parent)
                song.sub_genres.add(sub)

            # moods
            song.moods.clear()
            for mslug in item.get('moods', []):
                m = _get_or_create_mood(mslug)
                song.moods.add(m)

            # tags
            song.tags.clear()
            for tslug in item.get('tags', []):
                t = _get_or_create_tag(tslug)
                song.tags.add(t)

            if is_new:
                created += 1
                self.stdout.write(self.style.SUCCESS(f'Created song: {song}'))
            else:
                updated += 1
                self.stdout.write(self.style.WARNING(f'Updated song: {song}'))

        self.stdout.write(self.style.SUCCESS(f'Processing complete. Created: {created}, Updated: {updated}'))
