#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

prompt() {
  local label="$1"
  local default="${2:-}"
  local value
  if [ -n "$default" ]; then
    read -r -p "$label [$default]: " value
    printf '%s' "${value:-$default}"
  else
    read -r -p "$label: " value
    printf '%s' "$value"
  fi
}

prompt_required() {
  local label="$1"
  local value
  while true; do
    value="$(prompt "$label")"
    if [ -n "$value" ]; then
      printf '%s' "$value"
      return
    fi
    echo "This value is required." >&2
  done
}

prompt_secret() {
  local label="$1"
  local first second
  while true; do
    read -r -s -p "$label: " first
    echo >&2
    read -r -s -p "Confirm $label: " second
    echo >&2
    if [ -n "$first" ] && [ "$first" = "$second" ]; then
      printf '%s' "$first"
      return
    fi
    echo "Passwords do not match or are empty. Try again." >&2
  done
}

prompt_integer() {
  local label="$1"
  local default="$2"
  local value
  while true; do
    value="$(prompt "$label" "$default")"
    if [[ "$value" =~ ^[0-9]+$ ]] && [ "$value" -gt 0 ]; then
      printf '%s' "$value"
      return
    fi
    echo "Enter a positive integer." >&2
  done
}

prompt_port() {
  local label="$1"
  local default="$2"
  local value
  while true; do
    value="$(prompt "$label" "$default")"
    if [[ "$value" =~ ^[0-9]+$ ]] && [ "$value" -ge 1 ] && [ "$value" -le 65535 ]; then
      printf '%s' "$value"
      return
    fi
    echo "Enter a TCP port between 1 and 65535." >&2
  done
}

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 48 | tr -d '\n'
  else
    python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48), end="")
PY
  fi
}

random_suffix() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 5
  else
    python3 - <<'PY'
import secrets
print(secrets.token_hex(5), end="")
PY
  fi
}

normalize_host() {
  local host="$1"
  host="${host#http://}"
  host="${host#https://}"
  host="${host%%/*}"
  host="${host%.}"
  if [ -z "$host" ]; then
    echo "Host cannot be empty." >&2
    return 1
  fi
  if [[ "$host" == *:* ]]; then
    echo "Enter only the domain or IP here, without protocol or port." >&2
    return 1
  fi
  if ! [[ "$host" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$ || "$host" =~ ^[A-Za-z0-9]$ ]]; then
    echo "Invalid domain or IP: $host" >&2
    return 1
  fi
  printf '%s' "$host"
}

normalize_admin_path() {
  local path="$1"
  if [ -z "$path" ]; then
    path="/admin-$(random_suffix)"
  fi
  case "$path" in
    /*) ;;
    *) path="/$path" ;;
  esac
  path="${path%/}"
  if ! [[ "$path" =~ ^/[A-Za-z0-9][A-Za-z0-9_-]*(/[A-Za-z0-9][A-Za-z0-9_-]*)*$ ]]; then
    echo "Invalid admin path. Use letters, numbers, dashes and underscores only." >&2
    exit 1
  fi
  case "$path" in
    /|/index|/buy|/orders|/checkout|/payments|/order-query|/tickets)
      echo "Admin path conflicts with public routes: $path" >&2
      exit 1
      ;;
  esac
  printf '%s' "$path"
}

detect_public_ip() {
  curl -fsSL --max-time 3 https://api.ipify.org 2>/dev/null || true
}

require_command docker
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose Plugin is required. Install Docker first, then rerun this script." >&2
  exit 1
fi

echo "Card Issuance interactive setup"
echo
echo "Deployment mode:"
echo "  1) HTTPS with Caddy on ports 80/443 (recommended for domain deployment)"
echo "  2) HTTP direct port, for testing or manual reverse proxy"
while true; do
  mode="$(prompt "Choose mode" "1")"
  case "$mode" in
    1|https|HTTPS) mode="https"; break ;;
    2|http|HTTP) mode="http"; break ;;
    *) echo "Enter 1 or 2." ;;
  esac
done

app_name="$(prompt "Site name" "Card Issuance")"
order_expire_minutes="$(prompt_integer "Order expiration minutes" "15")"
admin_default="/admin-$(random_suffix)"
admin_path="$(normalize_admin_path "$(prompt "Admin path" "$admin_default")")"
admin_username="$(prompt_required "Admin login email")"
admin_password="$(prompt_secret "Admin password")"
app_secret="$(generate_secret)"

if [ "$mode" = "https" ]; then
  while true; do
    domain_raw="$(prompt_required "Domain, already pointing to this server")"
    if domain="$(normalize_host "$domain_raw")"; then
      break
    fi
  done
  acme_email="$(prompt "TLS certificate email, used by Let's Encrypt" "$admin_username")"
  app_base_url="https://$domain"
  app_port=""

  if [ -n "$acme_email" ]; then
    cat > Caddyfile <<EOF_CADDY
{
    email $acme_email
}

$domain {
    encode gzip
    reverse_proxy card-issuance:8080
}
EOF_CADDY
  else
    cat > Caddyfile <<EOF_CADDY
$domain {
    encode gzip
    reverse_proxy card-issuance:8080
}
EOF_CADDY
  fi

  cat > docker-compose.override.yml <<'EOF_COMPOSE'
services:
  caddy:
    image: caddy:2-alpine
    container_name: card-issuance-caddy
    restart: unless-stopped
    depends_on:
      - card-issuance
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - ./caddy_data:/data
      - ./caddy_config:/config
EOF_COMPOSE
else
  while true; do
    public_host_raw="$(prompt "Public domain or server IP" "$(detect_public_ip)")"
    if public_host="$(normalize_host "$public_host_raw")"; then
      break
    fi
  done
  app_port="$(prompt_port "Public HTTP port" "8080")"
  app_base_url="http://$public_host:$app_port"

  cat > docker-compose.override.yml <<EOF_COMPOSE
services:
  card-issuance:
    ports:
      - "0.0.0.0:$app_port:8080"
EOF_COMPOSE
fi

cat > .env <<EOF_ENV
APP_NAME=$app_name
APP_SECRET=$app_secret
APP_BASE_URL=$app_base_url
DATABASE_PATH=/data/card_issuance.sqlite3
ADMIN_PATH=$admin_path
ADMIN_USERNAME=$admin_username
ADMIN_PASSWORD=$admin_password
ORDER_EXPIRE_MINUTES=$order_expire_minutes
EOF_ENV
chmod 600 .env

mkdir -p data backups

docker compose up -d --build

echo
echo "Deployment complete."
echo "Frontend: $app_base_url/"
echo "Admin:    $app_base_url$admin_path/login"
if [ "$mode" = "https" ]; then
  echo
  echo "Required: DNS A/AAAA record for $domain must point to this server."
  echo "Required: ports 80 and 443 must be open in the VPS firewall/security group."
  echo "Visit without :8080. The correct URL is $app_base_url/"
  echo "If HTTPS is not ready yet, check: docker compose logs -f caddy"
else
  echo
  echo "Direct HTTP mode uses port $app_port."
  echo "Open TCP $app_port in the VPS firewall/security group, then visit $app_base_url/"
fi
