#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

if [ ! -d .git ]; then
  echo "This directory is not a Git repository. Clone the project from GitHub before using update.sh." >&2
  exit 1
fi

if [ ! -f .env ]; then
  echo ".env is missing. Copy .env.example to .env and fill production values first." >&2
  exit 1
fi

timestamp="$(date +%Y%m%d-%H%M%S)"
mkdir -p backups
if compgen -G "data/*.sqlite3*" > /dev/null; then
  tar -czf "backups/data-${timestamp}.tar.gz" data/*.sqlite3*
  echo "Database backup written to backups/data-${timestamp}.tar.gz"
else
  echo "No SQLite database files found under data/. Skipping backup."
fi

git pull --ff-only
docker compose up -d --build
docker image prune -f >/dev/null

echo "Update complete."
docker compose ps
