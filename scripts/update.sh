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
snapshot_dir="$(mktemp -d backups/.snapshot-XXXXXXXX)"
cleanup_snapshot() {
  rm -f "$snapshot_dir/data/card_issuance.sqlite3" "$snapshot_dir/archive.tar.gz"
  rmdir "$snapshot_dir/data" "$snapshot_dir" 2>/dev/null || true
}
trap cleanup_snapshot EXIT
mkdir "$snapshot_dir/data"
# Stream the script into the existing image and use its actual DATABASE_PATH.
# Only a completed SQLite online snapshot is archived, never live WAL files.
if ! docker compose exec -T card-issuance python - --output - \
  < scripts/backup.py > "$snapshot_dir/data/card_issuance.sqlite3"; then
  echo "数据库一致性备份失败，更新已停止。请确认应用正在运行并检查数据库配置。" >&2
  exit 1
fi
tar -czf "$snapshot_dir/archive.tar.gz" -C "$snapshot_dir" data/card_issuance.sqlite3
chmod 600 "$snapshot_dir/archive.tar.gz"
backup_path="backups/data-${timestamp}-${snapshot_dir##*-}.tar.gz"
mv "$snapshot_dir/archive.tar.gz" "$backup_path"
echo "数据库一致性备份已写入 $backup_path"
cleanup_snapshot
trap - EXIT

git pull --ff-only
docker compose up -d --build

app_base_url="$(get_env APP_BASE_URL)"
admin_path="$(get_env ADMIN_PATH)"
bash ./scripts/check-ready.sh "$app_base_url"
docker image prune -f >/dev/null

echo "更新完成。"
docker compose ps

if [ -n "$app_base_url" ]; then
  echo "前端：${app_base_url}/"
fi
if [ -n "$app_base_url" ] && [ -n "$admin_path" ]; then
  echo "管理后台：${app_base_url}${admin_path}/login"
fi
