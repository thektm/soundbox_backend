from django.core.management.base import BaseCommand
from django.db import connection


LOCK_ID = 904_272_611


class Command(BaseCommand):
    help = "Create non-blocking PostgreSQL indexes used by home and search reads."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            self.stdout.write("Search indexes skipped: PostgreSQL is required.")
            return

        connection.ensure_connection()
        connection.set_autocommit(True)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
            if not cursor.fetchone()[0]:
                self.stdout.write("Another search-index task is already running.")
                return

            try:
                try:
                    cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                except Exception as exc:
                    self.stderr.write(
                        f"pg_trgm extension could not be enabled; continuing with standard indexes: {exc}"
                    )

                statements = [
                    # Home ordering and public song filters.
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_song_home_release_idx "
                    "ON api_song (status, release_date DESC, created_at DESC)",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_song_home_plays_idx "
                    "ON api_song (status, plays DESC, created_at DESC)",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_playcount_created_at_idx "
                    "ON api_playcount (created_at DESC)",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_recommended_guest_rank_idx "
                    "ON api_recommendedplaylist (relevance_score DESC, created_at DESC) "
                    "WHERE user_id IS NULL",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_recommended_user_fresh_idx "
                    "ON api_recommendedplaylist (user_id, updated_at DESC, relevance_score DESC) "
                    "WHERE expires_at IS NOT NULL",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_recommended_cleanup_idx "
                    "ON api_recommendedplaylist (updated_at, id) "
                    "WHERE expires_at IS NOT NULL AND views = 0",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_otpcode_live_lookup_idx "
                    "ON api_otpcode (user_id, purpose, created_at DESC) "
                    "WHERE consumed = FALSE",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_refreshtoken_active_user_idx "
                    "ON api_refreshtoken (user_id, created_at DESC) "
                    "WHERE revoked_at IS NULL",
                    # Case-insensitive contains searches. PostgreSQL can combine these
                    # bitmap indexes across the endpoint's OR predicates.
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_song_title_trgm_idx "
                    "ON api_song USING gin (UPPER(title) gin_trgm_ops)",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_song_description_trgm_idx "
                    "ON api_song USING gin (UPPER(description) gin_trgm_ops)",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_song_lyrics_trgm_idx "
                    "ON api_song USING gin (UPPER(lyrics) gin_trgm_ops)",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_song_producers_trgm_idx "
                    "ON api_song USING gin (UPPER(producers::text) gin_trgm_ops)",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_song_composers_trgm_idx "
                    "ON api_song USING gin (UPPER(composers::text) gin_trgm_ops)",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_song_lyricists_trgm_idx "
                    "ON api_song USING gin (UPPER(lyricists::text) gin_trgm_ops)",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_artist_name_trgm_idx "
                    "ON api_artist USING gin (UPPER(name) gin_trgm_ops)",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_artist_artistic_name_trgm_idx "
                    "ON api_artist USING gin (UPPER(artistic_name) gin_trgm_ops)",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_artist_unique_id_trgm_idx "
                    "ON api_artist USING gin (UPPER(unique_id) gin_trgm_ops)",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_user_unique_id_trgm_idx "
                    "ON api_user USING gin (UPPER(unique_id) gin_trgm_ops)",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_user_first_name_trgm_idx "
                    "ON api_user USING gin (UPPER(first_name) gin_trgm_ops)",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_user_last_name_trgm_idx "
                    "ON api_user USING gin (UPPER(last_name) gin_trgm_ops)",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_user_roles_gin_idx "
                    "ON api_user USING gin (roles jsonb_path_ops)",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_artist_bio_trgm_idx "
                    "ON api_artist USING gin (UPPER(bio) gin_trgm_ops)",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_album_title_trgm_idx "
                    "ON api_album USING gin (UPPER(title) gin_trgm_ops)",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_album_description_trgm_idx "
                    "ON api_album USING gin (UPPER(description) gin_trgm_ops)",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_playlist_title_trgm_idx "
                    "ON api_playlist USING gin (UPPER(title) gin_trgm_ops)",
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS api_playlist_description_trgm_idx "
                    "ON api_playlist USING gin (UPPER(description) gin_trgm_ops)",
                ]

                completed = 0
                for statement in statements:
                    try:
                        cursor.execute(statement)
                        completed += 1
                    except Exception as exc:
                        # Index setup is an optimization and must never prevent startup.
                        self.stderr.write(f"Index skipped: {exc}")
    
                self.stdout.write(self.style.SUCCESS(f"Search index audit complete: {completed} ready."))
            finally:
                try:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])
                except Exception:
                    pass
