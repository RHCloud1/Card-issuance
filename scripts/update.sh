#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

echo "开始更新 Card Issuance..."

if [ ! -d .git ]; then
  echo "当前目录不是 Git 仓库。请先从 GitHub 克隆项目，再使用 update.sh。" >&2
  exit 1
fi

if [ ! -f .env ]; then
  echo "缺少 .env。请先将 .env.example 复制为 .env 并填入生产环境配置。" >&2
  exit 1
fi

if [ ! -f docker-compose.override.yml ]; then
  echo "缺少 docker-compose.override.yml。请先运行 ./scripts/setup.sh 选择 HTTPS 或直接 HTTP 部署方式。" >&2
  exit 1
fi

get_env() {
  local key="$1"
  grep -E "^${key}=" .env | tail -n 1 | cut -d= -f2- || true
}

timestamp="$(date +%Y%m%d-%H%M%S)"
mkdir -p backups
if compgen -G "data/*.sqlite3*" > /dev/null; then
  tar -czf "backups/data-${timestamp}.tar.gz" data/*.sqlite3*
  echo "数据库备份已写入 backups/data-${timestamp}.tar.gz"
else
  echo "data/ 下未找到 SQLite 数据库文件，跳过备份。"
fi

git pull --ff-only
docker compose up -d --build
docker image prune -f >/dev/null

echo "更新完成。"
docker compose ps

app_base_url="$(get_env APP_BASE_URL)"
admin_path="$(get_env ADMIN_PATH)"
if [ -n "$app_base_url" ]; then
  echo "前端：${app_base_url}/"
fi
if [ -n "$app_base_url" ] && [ -n "$admin_path" ]; then
  echo "管理后台：${app_base_url}${admin_path}/login"
fi
