#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

require_command() {
  local name="$1"
  local hint="${2:-}"
  if ! command -v "$name" >/dev/null 2>&1; then
    echo "缺少必需命令：$name" >&2
    if [ -n "$hint" ]; then
      echo "$hint" >&2
    fi
    exit 1
  fi
}

project_caddy_bindings_for_port() {
  local port="$1"

  if ! docker ps --format '{{.Names}}' | grep -Fx 'card-issuance-caddy' >/dev/null; then
    return 1
  fi

  docker inspect --format \
    "{{range (index .NetworkSettings.Ports \"${port}/tcp\")}}{{printf \"%s|%s\\n\" .HostIp .HostPort}}{{end}}" \
    card-issuance-caddy 2>/dev/null
}

normalize_listener_endpoint() {
  local endpoint="$1"
  local expected_port="$2"
  local host port

  if [[ "$endpoint" =~ ^\[([^]]+)\]:([0-9]+)$ ]]; then
    host="${BASH_REMATCH[1]}"
    port="${BASH_REMATCH[2]}"
  elif [[ "$endpoint" =~ ^(.+):([0-9]+)$ ]]; then
    host="${BASH_REMATCH[1]}"
    port="${BASH_REMATCH[2]}"
  else
    return 1
  fi

  if [ -z "$host" ] || [ "$host" = "*" ] || [ "$port" != "$expected_port" ]; then
    return 1
  fi

  printf '%s|%s' "$host" "$port"
}

binding_matches_listener() {
  local bindings="$1"
  local listener="$2"
  local expected_port="$3"
  local host host_port extra

  while IFS='|' read -r host host_port extra; do
    if [ -n "$extra" ] || [ -z "$host" ] || [ "$host_port" != "$expected_port" ]; then
      continue
    fi
    host="${host#[}"
    host="${host%]}"
    if [ "$host|$host_port" = "$listener" ]; then
      return 0
    fi
  done <<< "$bindings"

  return 1
}

project_caddy_owns_all_listeners() {
  local port="$1"
  local listeners="$2"
  local bindings line state receive_queue send_queue endpoint remainder listener

  if ! bindings="$(project_caddy_bindings_for_port "$port")" || [ -z "$bindings" ]; then
    return 1
  fi

  while IFS= read -r line; do
    [ -n "$line" ] || continue
    state=""
    receive_queue=""
    send_queue=""
    endpoint=""
    remainder=""
    read -r state receive_queue send_queue endpoint remainder <<< "$line"
    if [ -z "$endpoint" ]; then
      return 1
    fi
    if ! listener="$(normalize_listener_endpoint "$endpoint" "$port")"; then
      return 1
    fi
    if ! binding_matches_listener "$bindings" "$listener" "$port"; then
      return 1
    fi
  done <<< "$listeners"

  return 0
}

show_port_owners() {
  echo "80/443 端口监听情况：" >&2
  ss -ltnp '( sport = :80 or sport = :443 )' 2>&1 || true
  echo "相关 Docker 容器：" >&2
  docker ps --format 'table {{.Names}}\t{{.Ports}}' 2>&1 || true
}

check_https_ports() {
  local port listeners
  if ! command -v ss >/dev/null 2>&1; then
    echo "警告：未找到 ss，无法检查 HTTPS 端口占用情况，将继续部署。" >&2
    return 0
  fi

  for port in 80 443; do
    if ! listeners="$(ss -ltnH "sport = :$port" 2>/dev/null)"; then
      echo "无法读取端口 $port 的监听信息，已停止 HTTPS 部署。" >&2
      show_port_owners
      return 1
    fi
    if [ -n "$listeners" ]; then
      if project_caddy_owns_all_listeners "$port" "$listeners"; then
        continue
      fi
      echo "端口 $port 已被占用，无法启动 HTTPS 部署。" >&2
      show_port_owners
      return 1
    fi
  done
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
    echo "此项为必填项。" >&2
  done
}

prompt_secret() {
  local label="$1"
  local first second
  while true; do
    read -r -s -p "$label: " first
    echo >&2
    read -r -s -p "确认$label: " second
    echo >&2
    if [ -n "$first" ] && [ "$first" = "$second" ]; then
      printf '%s' "$first"
      return
    fi
    echo "两次密码不一致或密码为空，请重试。" >&2
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
    echo "请输入正整数。" >&2
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
    echo "请输入 1 到 65535 之间的 TCP 端口。" >&2
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
    echo "主机名不能为空。" >&2
    return 1
  fi
  if [[ "$host" == *:* ]]; then
    echo "请只输入域名或 IP 地址，不含协议和端口。" >&2
    return 1
  fi
  if ! [[ "$host" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$ || "$host" =~ ^[A-Za-z0-9]$ ]]; then
    echo "域名或 IP 地址无效：$host" >&2
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
    echo "管理路径无效。只能使用字母、数字、连字符和下划线。" >&2
    exit 1
  fi
  case "$path" in
    /|/index|/buy|/orders|/checkout|/payments|/order-query|/tickets)
      echo "管理路径与公开路由冲突：$path" >&2
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
  echo "需要 Docker Compose 插件。请先安装 Docker，然后重新运行此脚本。" >&2
  exit 1
fi

echo "Card Issuance 交互式部署"
echo
echo "部署模式："
echo "  1) 使用 Caddy 的 HTTPS（端口 80/443，推荐域名部署）"
echo "  2) HTTP 直连端口（用于测试或手动反向代理）"
while true; do
  mode="$(prompt "请选择模式" "1")"
  case "$mode" in
    1|https|HTTPS) mode="https"; break ;;
    2|http|HTTP) mode="http"; break ;;
    *) echo "请输入 1 或 2。" ;;
  esac
done

if [ "$mode" = "https" ]; then
  require_command curl "HTTPS 部署需要 curl。请先安装 curl 后重新运行此脚本。"
  check_https_ports
fi

app_name="$(prompt "站点名称" "Card Issuance")"
order_expire_minutes="$(prompt_integer "订单过期分钟数" "15")"
admin_default="/admin-$(random_suffix)"
admin_path="$(normalize_admin_path "$(prompt "管理路径" "$admin_default")")"
admin_username="$(prompt_required "管理员登录邮箱")"
admin_password="$(prompt_secret "管理员密码")"
app_secret="$(generate_secret)"

if [ "$mode" = "https" ]; then
  while true; do
    domain_raw="$(prompt_required "已解析到此服务器的域名")"
    if domain="$(normalize_host "$domain_raw")"; then
      break
    fi
  done
  acme_email="$(prompt "TLS 证书邮箱（供 Let's Encrypt 使用）" "$admin_username")"
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
    dns:
      - "${CONTAINER_DNS_PRIMARY:-1.1.1.1}"
      - "${CONTAINER_DNS_SECONDARY:-8.8.8.8}"
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
    public_host_raw="$(prompt "公开域名或服务器 IP" "$(detect_public_ip)")"
    if public_host="$(normalize_host "$public_host_raw")"; then
      break
    fi
  done
  app_port="$(prompt_port "公开 HTTP 端口" "8080")"
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
CONTAINER_DNS_PRIMARY=1.1.1.1
CONTAINER_DNS_SECONDARY=8.8.8.8
EOF_ENV
chmod 600 .env

mkdir -p data backups

docker compose up -d --build

if [ "$mode" = "https" ]; then
  https_ready=false
  for ((attempt = 1; attempt <= 12; attempt++)); do
    if curl --silent --show-error --fail --head \
      --max-time 5 \
      --resolve "$domain:443:127.0.0.1" \
      "https://$domain/"; then
      https_ready=true
      break
    fi
    sleep 5
  done

  if [ "$https_ready" != true ]; then
    echo "HTTPS 尚未就绪，已达到等待上限。保留容器以便诊断。" >&2
    echo "最近的 Caddy 日志：" >&2
    docker compose logs --tail=100 caddy >&2 || true
    echo "请运行 ./scripts/diagnose.sh 继续诊断。" >&2
    exit 1
  fi
fi

echo
echo "部署完成。"
echo "前台：$app_base_url/"
echo "管理后台：$app_base_url$admin_path/login"
if [ "$mode" = "https" ]; then
  echo
  echo "请确认 $domain 的 DNS A/AAAA 记录已指向此服务器。"
  echo "请确认 VPS 防火墙或安全组已开放 80 和 443 端口。"
  echo "访问时无需添加 :8080，正确地址为 $app_base_url/"
  echo "若 HTTPS 尚未就绪，请检查：docker compose logs -f caddy"
else
  echo
  echo "HTTP 直连模式使用端口 $app_port。"
  echo "请在 VPS 防火墙或安全组中开放 TCP $app_port，然后访问 $app_base_url/"
fi
