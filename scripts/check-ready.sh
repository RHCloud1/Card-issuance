#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
app_base_url="${1:-}"

app_ready=false
for ((attempt = 1; attempt <= 12; attempt++)); do
  if docker compose exec -T card-issuance python -c \
    'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/", timeout=3).close()'; then
    app_ready=true
    break
  fi
  sleep 5
done

if [ "$app_ready" != true ]; then
  echo "应用 HTTP 服务尚未就绪，已达到等待上限。保留容器以便诊断。" >&2
  docker compose logs --tail=100 card-issuance >&2 || true
  echo "请运行 ./scripts/diagnose.sh 继续诊断。" >&2
  exit 1
fi

if [[ "$app_base_url" == https://* ]] && docker compose config --services | grep -Fx caddy >/dev/null; then
  if ! command -v curl >/dev/null 2>&1; then
    echo "未找到 curl，无法确认 HTTPS 是否就绪。" >&2
    exit 1
  fi
  domain="${app_base_url#https://}"
  domain="${domain%%/*}"
  https_ready=false
  https_error=""
  # 首次申请证书可能需要一两分钟，期间握手失败属正常，只在最终失败时显示错误。
  echo "正在等待 HTTPS 证书签发（首次部署可能需要一两分钟）..."
  for ((attempt = 1; attempt <= 24; attempt++)); do
    if https_error="$(curl --silent --show-error --fail --head \
      --max-time 5 \
      --resolve "$domain:443:127.0.0.1" \
      --output /dev/null \
      "https://$domain/" 2>&1)"; then
      https_ready=true
      break
    fi
    sleep 5
  done
  if [ "$https_ready" != true ]; then
    echo "HTTPS 尚未就绪，已达到等待上限。保留容器以便诊断。" >&2
    echo "最后一次检查的错误：$https_error" >&2
    docker compose logs --tail=100 caddy >&2 || true
    echo "请运行 ./scripts/diagnose.sh 继续诊断。" >&2
    exit 1
  fi
fi

echo "应用服务检查通过。"
