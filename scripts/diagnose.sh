#!/usr/bin/env bash
set -u

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

section() {
  printf '\n== %s ==\n' "$1"
}

get_env() {
  local key="$1"
  if [ -f .env ]; then
    grep -E "^${key}=" .env | tail -n 1 | cut -d= -f2- || true
  fi
}

run() {
  "$@" || true
}

app_base_url="$(get_env APP_BASE_URL)"
admin_path="$(get_env ADMIN_PATH)"

section "Deployment config"
echo "APP_BASE_URL=${app_base_url:-missing}"
if [ -n "$admin_path" ]; then
  echo "ADMIN_PATH is configured"
else
  echo "ADMIN_PATH is missing"
fi
if [ -f Caddyfile ]; then
  echo "Mode: HTTPS with Caddy"
else
  echo "Mode: direct HTTP or external reverse proxy"
fi

section "Docker status"
run docker compose ps

section "Application logs"
run docker compose logs --tail=80 card-issuance

if [ -f Caddyfile ]; then
  section "Caddy logs"
  run docker compose logs --tail=120 caddy
fi

section "Listening ports"
if command -v ss >/dev/null 2>&1; then
  run sh -c "ss -lntp | grep -E ':(80|443|8080)\\b'"
else
  echo "ss is not installed."
fi

section "HTTP checks"
if command -v curl >/dev/null 2>&1; then
  if [[ "$app_base_url" == https://* ]]; then
    domain="${app_base_url#https://}"
    domain="${domain%%/*}"
    echo "Testing HTTPS through local Caddy with Host/SNI: $domain"
    run curl -k -I --max-time 10 --resolve "$domain:443:127.0.0.1" "https://$domain/"
  elif [ -n "$app_base_url" ]; then
    echo "Testing configured URL: $app_base_url/"
    run curl -I --max-time 10 "$app_base_url/"
  else
    echo "APP_BASE_URL is missing; skip URL check."
  fi
else
  echo "curl is not installed."
fi

section "DNS hint"
if [[ "$app_base_url" == https://* || "$app_base_url" == http://* ]]; then
  host="${app_base_url#http://}"
  host="${host#https://}"
  host="${host%%/*}"
  host="${host%%:*}"
  if command -v getent >/dev/null 2>&1; then
    run getent hosts "$host"
  else
    echo "getent is not installed."
  fi
fi

echo
echo "If HTTPS mode is used, the browser URL should be https://domain/ without :8080."
echo "If direct HTTP mode is used, the browser URL should be http://domain:port/ and that TCP port must be open."
