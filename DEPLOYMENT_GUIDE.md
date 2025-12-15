# SoundBox Backend - Deployment Guide

## Initial Setup

After deploying the code to your server, run these commands:

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Run Migrations
```bash
python manage.py migrate
```

### 3. Create Initial Data
```bash
python manage.py create_initial_data
```

This will create:
- 10 main genres with 4 sub-genres each (50 total)
- 15 moods
- 20 tags

### 4. Create Superuser (Optional)
```bash
python manage.py createsuperuser
```

### 5. Collect Static Files (For Production)
```bash
python manage.py collectstatic --noinput
```

### 6. Run Server
```bash
# Development
python manage.py runserver

# Production (with Gunicorn)
gunicorn soundbox.wsgi:application --bind 0.0.0.0:8000
```

## Available Management Commands

- `create_initial_data` - Creates all genres, moods, and tags
- `create_genres` - Creates only genres and sub-genres
- `create_moods` - Creates only moods
- `create_tags` - Creates only tags

## API Endpoints

See `POSTMAN_TESTING_GUIDE.md` for complete API documentation and testing instructions.

## Environment Variables

Make sure to set these environment variables in production:

- `DJANGO_SECRET`
- `DEBUG=False`
- `R2_ENDPOINT_URL`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_BUCKET_NAME`
- `R2_CDN_BASE`
- `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`