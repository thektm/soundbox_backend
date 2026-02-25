Artist Album Songs endpoints

These endpoints allow an authenticated artist to assign or remove existing songs to/from one of their albums.

Base path: /api/

Authentication: Bearer token (artist user). The authenticated user must have an artist profile and must own both the album and the songs.

1) Assign songs to album (POST)

Endpoint:
POST /artist/albums/<album_id>/songs/

Headers:
- Authorization: Bearer <ACCESS_TOKEN>
- Content-Type: application/json

Body (JSON):
{
  "song_ids": [123, 456]
}

Behavior:
- Only songs that belong to the authenticated artist will be updated.
- Matching songs will have their `album` set to the specified album.
- Response contains `updated_count`, `updated_ids`, `missing_or_not_owned_ids`, and `songs` (serialized).

Example request (curl):

curl -X POST "https://api.example.com/artist/albums/42/songs/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"song_ids": [12,34]}'

Example response (200):
{
  "updated_count": 2,
  "updated_ids": [12,34],
  "missing_or_not_owned_ids": [],
  "songs": [ /* song objects */ ]
}

2) Remove songs from album (DELETE)

Endpoint:
DELETE /artist/albums/<album_id>/songs/

Headers:
- Authorization: Bearer <ACCESS_TOKEN>
- Content-Type: application/json

Body (JSON):
{
  "song_ids": [123, 456]
}

Behavior:
- Only songs that belong to the artist and are currently assigned to the given album will be affected.
- Matching songs will have their `album` unset (null).
- Response contains `removed_count`, `removed_ids`, and `missing_or_not_owned_or_not_in_album`.

Example request (curl):

curl -X DELETE "https://api.example.com/artist/albums/42/songs/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"song_ids": [12,34]}'

Example response (200):
{
  "removed_count": 2,
  "removed_ids": [12,34],
  "missing_or_not_owned_or_not_in_album": []
}

Notes:
- If you want to add a new song file to an album during album creation, use the existing `artist/albums/` POST which supports `existing_song_ids` and multipart new song fields.
- This endpoint only operates on existing songs (no file upload).