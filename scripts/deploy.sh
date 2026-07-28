#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

get_env() {
  local key="$1"
  local file="${2:-.env}"
  if [ -f "$file" ]; then
    grep -E "^${key}=" "$file" | tail -n 1 | cut -d= -f2- || true
  fi
}

if [ ! -f .env ] || [ ! -f docker-compose.override.yml ]; then
  echo "Interactive setup is required before deployment."
  exec ./scripts/setup.sh
fi

docker compose up -d --build

app_base_url="$(get_env APP_BASE_URL)"
admin_path="$(get_env ADMIN_PATH)"

echo
echo "Deployment complete."
echo "Frontend: ${app_base_url:-http://127.0.0.1:8080}/"
if [ -n "$app_base_url" ] && [ -n "$admin_path" ]; then
  echo "Admin:    ${app_base_url}${admin_path}/login"
else
  echo "Admin:    check APP_BASE_URL and ADMIN_PATH in .env"
fi
