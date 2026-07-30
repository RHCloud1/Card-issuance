#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

echo "开始部署 Card Issuance..."

get_env() {
  local key="$1"
  local file="${2:-.env}"
  if [ -f "$file" ]; then
    grep -E "^${key}=" "$file" | tail -n 1 | cut -d= -f2- || true
  fi
}

if [ ! -f .env ] || [ ! -f docker-compose.override.yml ]; then
  echo "部署前需要先完成交互式配置。"
  exec ./scripts/setup.sh
fi

docker compose up -d --build

app_base_url="$(get_env APP_BASE_URL)"
admin_path="$(get_env ADMIN_PATH)"

echo
echo "部署完成。"
echo "前端：${app_base_url:-http://127.0.0.1:8080}/"
if [ -n "$app_base_url" ] && [ -n "$admin_path" ]; then
  echo "管理后台：${app_base_url}${admin_path}/login"
else
  echo "管理后台：请检查 .env 中的 APP_BASE_URL 和 ADMIN_PATH。"
fi
