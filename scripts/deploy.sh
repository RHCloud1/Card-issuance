#!/usr/bin/env bash
set -euo pipefail

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example."
  echo "Edit APP_SECRET, ADMIN_USERNAME, ADMIN_PASSWORD and APP_BASE_URL before public launch."
fi

docker compose up -d --build
echo "Card issuance system is running on http://127.0.0.1:8080"
