#!/usr/bin/env bash
# Helper script to run the Django management command `create_songs`.
# Usage:
#   ./create_songs.sh                # uses deploy/songs_seed.json by default
#   ./create_songs.sh path/to/file.json

PYTHON=""
if [ -f ".venv/Scripts/activate" ] || [ -f ".venv/bin/activate" ]; then
  # Use virtualenv python if present
  if [ -f ".venv/Scripts/python.exe" ]; then
    PYTHON=".venv/Scripts/python.exe"
  else
    PYTHON=".venv/bin/python"
  fi
else
  PYTHON=$(which python || which python3)
fi

if [ -z "$PYTHON" ]; then
  echo "No python found. Please ensure python is installed or activate your virtualenv." && exit 1
fi

SEED_FILE=${1:-deploy/songs_seed.json}

echo "Running create_songs using seed: $SEED_FILE"
"$PYTHON" manage.py create_songs --file "$SEED_FILE"
