# soundBox backend (Django)

This is a minimal Django REST API skeleton for the `soundBox` project.

Quick start (Windows PowerShell):

1. Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies:

```powershell
pip install -r requirements.txt
```

3. PostgreSQL setup

- Install PostgreSQL on your machine (https://www.postgresql.org/download/).
- Create a database and user or use the default `postgres` user. Example (psql):

```sql
CREATE DATABASE soundbox;
CREATE USER soundbox_user WITH PASSWORD 'yourpassword';
GRANT ALL PRIVILEGES ON DATABASE soundbox TO soundbox_user;
```

4. Set environment variables (PowerShell example):

```powershell
#$ for current session only
$env:DB_NAME = 'soundbox'
$env:DB_USER = 'soundbox_user'
$env:DB_PASSWORD = 'yourpassword'
$env:DB_HOST = 'localhost'
$env:DB_PORT = '5432'

# then run migrations
python manage.py migrate

# create superuser (optional)
python manage.py createsuperuser

# start dev server
python manage.py runserver
```

If you prefer not to create a DB/user manually, set the `DB_*` environment variables to match your existing Postgres setup. `psycopg2-binary` is included in `requirements.txt` for easy installation.

API endpoints:

- `GET /api/tracks/` - list tracks
- `GET /api/tracks/{id}/` - retrieve track
- `POST /api/tracks/` - create track
- `PUT/PATCH /api/tracks/{id}/` - update track
- `DELETE /api/tracks/{id}/` - delete track

If you want, I can add authentication, file uploads, or more models next.

Docker deploy (Ubuntu)

1. Create an `.env` file on the server (outside of version control) with values for at least:

```
DJANGO_DEBUG=False
DJANGO_SECRET=your_production_secret
DB_NAME=soundbox_db
DB_USER=soundbox_user
DB_PASSWORD=postgres
DB_HOST=127.0.0.1
DB_PORT=5432
```

Local Windows helper

- A convenience `.env.win` is included for Windows/Docker Desktop development. To run locally on Windows, copy or rename it to `.env` in the `backend/` directory before `docker compose up`:

```powershell
# from backend/ on Windows
cp .env.win .env
docker compose up -d
```

- `.env.win` uses `host.docker.internal` as `DB_HOST` so containers can reach a Postgres instance running on the Windows host. If you run Postgres in a container, prefer adding a `postgres` service to `docker-compose.yml` and using the service name as `DB_HOST`.

2. Build and run with Docker Compose:

```bash
# from the backend/ directory on the server
docker compose build --pull
docker compose up -d
```

3. The `entrypoint.sh` runs migrations and `collectstatic` automatically, then starts `gunicorn` on port `8000`.

Notes & recommendations:

- Keep `.env` out of source control and use a secrets manager for production values.
- If Postgres runs on the same host, `DB_HOST=127.0.0.1` will work if using the host network or if Postgres is accessible to the container; otherwise set the host IP or hostname reachable from the container.
- You may want to run the container behind a reverse proxy (nginx) for TLS termination and static file serving.
