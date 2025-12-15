# SoundBox API - Postman Testing Guide

Base URL: `https://api.sedabox.com`

## 1. Authentication Endpoints

### Register User
- **Method**: POST
- **URL**: `https://api.sedabox.com/register/`
- **Body** (raw JSON):
```json
{
  "phone_number": "09123456789",
  "password": "yourpassword",
  "roles": "artist",
  "first_name": "John",
  "last_name": "Doe"
}
```

### Login
- **Method**: POST
- **URL**: `https://api.sedabox.com/login/`
- **Body** (raw JSON):
```json
{
  "phone_number": "09123456789",
  "password": "yourpassword"
}
```
- **Response**: Save the `access` token for authenticated requests

### Refresh Token
- **Method**: POST
- **URL**: `https://api.sedabox.com/refresh/`
- **Body** (raw JSON):
```json
{
  "refresh": "your_refresh_token_here"
}
```

---

## 2. Artist Endpoints

### Create Artist
- **Method**: POST
- **URL**: `https://api.sedabox.com/artists/`
- **Headers**: 
  - `Authorization`: `Bearer YOUR_ACCESS_TOKEN`
- **Body** (raw JSON):
```json
{
  "name": "Mohsen Yeganeh",
  "bio": "Iranian singer and composer",
  "verified": false
}
```

### List All Artists
- **Method**: GET
- **URL**: `https://api.sedabox.com/artists/`
- No authentication required

### Get Single Artist
- **Method**: GET
- **URL**: `https://api.sedabox.com/artists/{id}/`

---

## 3. Genre, Mood, Tag, SubGenre Endpoints

### Create Genre
- **Method**: POST
- **URL**: `https://api.sedabox.com/genres/`
- **Headers**: `Authorization: Bearer YOUR_ACCESS_TOKEN`
- **Body** (raw JSON):
```json
{
  "name": "Pop",
  "slug": "pop"
}
```

### Create SubGenre
- **Method**: POST
- **URL**: `https://api.sedabox.com/subgenres/`
- **Headers**: `Authorization: Bearer YOUR_ACCESS_TOKEN`
- **Body** (raw JSON):
```json
{
  "name": "Synth Pop",
  "slug": "synth-pop",
  "parent_genre": 1
}
```

### Create Mood
- **Method**: POST
- **URL**: `https://api.sedabox.com/moods/`
- **Headers**: `Authorization: Bearer YOUR_ACCESS_TOKEN`
- **Body** (raw JSON):
```json
{
  "name": "Happy",
  "slug": "happy"
}
```

### Create Tag
- **Method**: POST
- **URL**: `https://api.sedabox.com/tags/`
- **Headers**: `Authorization: Bearer YOUR_ACCESS_TOKEN`
- **Body** (raw JSON):
```json
{
  "name": "Summer Hit",
  "slug": "summer-hit"
}
```

### List All (No Auth Required)
- `GET https://api.sedabox.com/genres/`
- `GET https://api.sedabox.com/subgenres/`
- `GET https://api.sedabox.com/moods/`
- `GET https://api.sedabox.com/tags/`

---

## 4. Album Endpoints

### Create Album
- **Method**: POST
- **URL**: `https://api.sedabox.com/albums/`
- **Headers**: `Authorization: Bearer YOUR_ACCESS_TOKEN`
- **Body** (raw JSON):
```json
{
  "title": "Delkhoshi",
  "artist": 1,
  "release_date": "2024-03-20",
  "description": "Amazing album"
}
```

### List Albums
- **Method**: GET
- **URL**: `https://api.sedabox.com/albums/`

---

## 5. Song Upload (Most Important!)

### Upload Song with Audio File
- **Method**: POST
- **URL**: `https://api.sedabox.com/songs/upload/`
- **Headers**: 
  - `Authorization`: `Bearer YOUR_ACCESS_TOKEN`
- **Body** (form-data):
  
  | Key | Type | Value | Required |
  |-----|------|-------|----------|
  | audio_file | File | Select your .mp3 or .wav file | ✅ Yes |
  | cover_image | File | Select cover image | ❌ No |
  | title | Text | "Delkhoshi" | ✅ Yes |
  | artist_id | Text | 1 | ✅ Yes |
  | featured_artists | Text | ["Artist Name"] | ❌ No |
  | album_id | Text | 1 | ❌ No |
  | is_single | Text | true | ❌ No |
  | release_date | Text | 2024-03-20 | ❌ No |
  | language | Text | fa | ❌ No |
  | description | Text | "Song description" | ❌ No |
  | lyrics | Text | "Song lyrics..." | ❌ No |
  | genre_ids | Text | [1, 2] | ❌ No |
  | sub_genre_ids | Text | [1] | ❌ No |
  | mood_ids | Text | [1, 2] | ❌ No |
  | tag_ids | Text | [1] | ❌ No |
  | tempo | Text | 120 | ❌ No |
  | energy | Text | 75 | ❌ No |
  | danceability | Text | 80 | ❌ No |
  | valence | Text | 90 | ❌ No |
  | acousticness | Text | 30 | ❌ No |
  | instrumentalness | Text | 10 | ❌ No |
  | speechiness | Text | 20 | ❌ No |
  | live_performed | Text | false | ❌ No |
  | label | Text | "Record Label" | ❌ No |
  | producers | Text | ["Producer 1", "Producer 2"] | ❌ No |
  | composers | Text | ["Composer Name"] | ❌ No |
  | lyricists | Text | ["Lyricist Name"] | ❌ No |
  | credits | Text | "Additional credits" | ❌ No |

**Important Notes:**
- For arrays (featured_artists, genre_ids, etc.), use JSON format: `["value1", "value2"]` or `[1, 2, 3]`
- The file will be uploaded to R2 with format: `Artist - Title (feat. Featured)` or `Artist - Title`
- Duration is automatically extracted from the audio file
- Original format is automatically detected

---

## 6. Song Management Endpoints

### List All Published Songs (No Auth)
- **Method**: GET
- **URL**: `https://api.sedabox.com/songs/`

### Get Single Song
- **Method**: GET
- **URL**: `https://api.sedabox.com/songs/{id}/`

### Update Song (Full Update)
- **Method**: PUT
- **URL**: `https://api.sedabox.com/songs/{id}/`
- **Headers**: `Authorization: Bearer YOUR_ACCESS_TOKEN`
- **Body** (raw JSON):
```json
{
  "title": "Updated Title",
  "artist": 1,
  "featured_artists": ["Featured Artist"],
  "audio_file": "https://cdn.sedabox.com/songs/existing.mp3",
  "status": "published",
  "genre_ids": [1, 2],
  "sub_genre_ids": [1],
  "mood_ids": [1],
  "tag_ids": [1]
}
```

### Partial Update Song
- **Method**: PATCH
- **URL**: `https://api.sedabox.com/songs/{id}/`
- **Headers**: `Authorization: Bearer YOUR_ACCESS_TOKEN`
- **Body** (raw JSON):
```json
{
  "status": "published",
  "plays": 1000
}
```

### Delete Song
- **Method**: DELETE
- **URL**: `https://api.sedabox.com/songs/{id}/`
- **Headers**: `Authorization: Bearer YOUR_ACCESS_TOKEN`

### Increment Play Count
- **Method**: POST
- **URL**: `https://api.sedabox.com/songs/{id}/increment_plays/`
- **Headers**: `Authorization: Bearer YOUR_ACCESS_TOKEN`
- No body required

---

## 7. Filter & Search Examples

### Filter Songs by Status (Admin Only)
- `GET https://api.sedabox.com/songs/?status=published`
- `GET https://api.sedabox.com/songs/?status=draft`

### Filter by Artist
- `GET https://api.sedabox.com/songs/?artist=1`

### Filter by Genre
- `GET https://api.sedabox.com/songs/?genres=1`

---

## 8. Generic R2 Upload (For Testing)

### Upload Any File to R2
- **Method**: POST
- **URL**: `https://api.sedabox.com/upload/`
- **Body** (form-data):
  - `file`: Select file
  - `folder`: (optional) e.g., "test"
  - `filename`: (optional) custom filename

---

## Testing Flow Example

1. **Register/Login** → Get access token
2. **Create Artist** → Note the artist ID (e.g., 1)
3. **Create Genre** → Note genre ID (e.g., 1)
4. **Create SubGenre** → Link to genre, note ID (e.g., 1)
5. **Create Mood** → Note mood ID (e.g., 1)
6. **Create Tag** → Note tag ID (e.g., 1)
7. **Create Album** (optional) → Note album ID
8. **Upload Song** → Use form-data with:
   - audio_file: your.mp3
   - title: "Test Song"
   - artist_id: 1
   - genre_ids: [1]
   - sub_genre_ids: [1]
   - mood_ids: [1]
   - tag_ids: [1]
9. **List Songs** → Verify your upload
10. **Update Song Status** → Change to "published"

---

## Common Response Codes

- `200 OK` - Success
- `201 Created` - Resource created successfully
- `400 Bad Request` - Validation error (check response for details)
- `401 Unauthorized` - Missing or invalid token
- `403 Forbidden` - No permission
- `404 Not Found` - Resource doesn't exist
- `500 Internal Server Error` - Server error

---

## Tips for Postman

1. **Save your access token** in an environment variable:
   - Create environment
   - Add variable: `access_token`
   - Use: `{{access_token}}` in Authorization headers

2. **Create a Collection** with all endpoints

3. **Use Pre-request Scripts** to auto-refresh tokens

4. **Test Scripts** to save response data:
```javascript
// Save artist ID after creation
pm.environment.set("artist_id", pm.response.json().id);
```

5. For **form-data arrays**, use the raw JSON format in the value field
