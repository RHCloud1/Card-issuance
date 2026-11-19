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


# 与应用启动检查保持一致（app/main.py WEAK_ADMIN_PASSWORDS），避免装完才发现容器拒绝启动。
WEAK_ADMIN_PASSWORDS=(change-me-now change-this-admin-password admin password)

input_closed() {
  echo >&2
  echo "输入已结束，安装已中止。" >&2
  exit 1
}

# ask <变量名> <提示> [默认值]：结果直接写入变量，不经过子 shell，输入结束时能真正中止脚本。
ask() {
  local __var="$1" __label="$2" __default="${3:-}" __value
  if [ -n "$__default" ]; then
    read -r -p "$__label [$__default]: " __value || input_closed
    __value="${__value:-$__default}"
  else
    read -r -p "$__label: " __value || input_closed
  fi
  printf -v "$__var" '%s' "$__value"
}

# ask_yes_no <提示> <默认 y|n>：回答“是”返回 0。
ask_yes_no() {
  local label="$1" default="$2" hint answer
  if [ "$default" = y ]; then hint="Y/n"; else hint="y/N"; fi
  while true; do
    read -r -p "$label [$hint]: " answer || input_closed
    case "${answer:-$default}" in
      y|Y|yes|YES|是) return 0 ;;
      n|N|no|NO|否) return 1 ;;
    esac
    echo "请输入 y 或 n。" >&2
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

# Compose interprets $ and double-quoted escape sequences while loading .env.
dotenv_literal() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//\$/\$\$}"
  printf '"%s"' "$value"
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
  host="${host,,}"
  if [ -z "$host" ]; then
    echo "主机名不能为空。" >&2
    return 1
  fi
  if [[ "$host" == *:* ]]; then
    echo "请只输入域名或 IP 地址，不含协议和端口。" >&2
    return 1
  fi
  if ! [[ "$host" =~ ^[a-z0-9][a-z0-9.-]*[a-z0-9]$ || "$host" =~ ^[a-z0-9]$ ]]; then
    echo "域名或 IP 地址无效：$host" >&2
    return 1
  fi
  printf '%s' "$host"
}

detect_public_ip() {
  curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true
}

detect_public_ipv6() {
  curl -6 -fsS --max-time 5 https://api6.ipify.org 2>/dev/null || true
}

get_env() {
  grep -E "^$1=" .env 2>/dev/null | tail -n 1 | cut -d= -f2- || true
}

# 证书申请前核对域名解析：解析不到或指向别处时 Caddy 会反复申请失败，
# 很快触发 Let's Encrypt 的失败次数限流（每小时 5 次），之后即使改好 DNS 也要等。
verify_domain_points_here() {
  local domain="$1" records4 records6 public4 public6 problems=0
  if ! command -v getent >/dev/null 2>&1; then
    echo "未找到 getent，跳过域名解析核对。" >&2
    return 0
  fi
  records4="$(getent ahostsv4 "$domain" 2>/dev/null | awk '{print $1}' | sort -u || true)"
  # glibc 在没有 AAAA 记录时会返回 ::ffff: 开头的 IPv4 映射地址，需要排除。
  records6="$(getent ahostsv6 "$domain" 2>/dev/null | awk '{print $1}' | grep -v '^::ffff:' | sort -u || true)"
  if [ -z "$records4$records6" ]; then
    echo "域名 $domain 目前解析不到任何地址。请先在 DNS 服务商添加 A 记录指向本服务器，等解析生效后再部署。" >&2
    return 1
  fi
  public4="$(detect_public_ip)"
  public6="$(detect_public_ipv6)"
  echo "域名 $domain 解析到：$(echo $records4 $records6)"
  echo "本服务器公网 IP：$(echo ${public4:-未知IPv4} $public6)"
  if [ -z "$public4$public6" ]; then
    echo "无法获取本服务器公网 IP，跳过解析核对。" >&2
    return 0
  fi
  if [ -n "$records4" ] && [ -n "$public4" ] && ! grep -Fxq -- "$public4" <<< "$records4"; then
    echo "A 记录没有指向本服务器（$public4）。若开启了 Cloudflare 等 CDN 代理，请先改为“仅 DNS”再申请证书。" >&2
    problems=1
  fi
  if [ -n "$records6" ] && { [ -z "$public6" ] || ! grep -Fxq -- "$public6" <<< "$records6"; }; then
    echo "域名有 AAAA（IPv6）记录，但没有指向本服务器的 IPv6。Let's Encrypt 会优先通过 IPv6 验证，请删除或修正 AAAA 记录。" >&2
    problems=1
  fi
  return "$problems"
}

ask_https_domain() {
  local raw
  while true; do
    ask raw "已解析到本服务器的域名（如 shop.example.com）"
    domain="$(normalize_host "$raw")" || continue
    if [[ "$domain" =~ ^[0-9.]+$ ]]; then
      echo "HTTPS 模式需要域名，不能直接使用 IP。只有 IP 时请选择 HTTP 直连模式。" >&2
      continue
    fi
    if [[ "$domain" != *.* ]]; then
      echo "域名格式无效：$domain" >&2
      continue
    fi
    if verify_domain_points_here "$domain"; then
      return
    fi
    if ask_yes_no "仍要用这个域名继续吗？证书很可能申请失败" n; then
      return
    fi
  done
}

# 端口被其他程序监听时返回 0。重新配置时本项目的应用容器自己占着的端口不算冲突。
port_in_use() {
  local port="$1" listeners
  command -v ss >/dev/null 2>&1 || return 1
  listeners="$(ss -ltnH "sport = :$port" 2>/dev/null || true)"
  [ -n "$listeners" ] || return 1
  if docker port card-issuance 8080/tcp 2>/dev/null | grep -Eq ":${port}\$"; then
    return 1
  fi
  return 0
}

ask_port() {
  local target="$1" label="$2" port_value
  while true; do
    ask port_value "$label" "8080"
    if ! [[ "$port_value" =~ ^[0-9]+$ ]] || [ "$port_value" -lt 1 ] || [ "$port_value" -gt 65535 ]; then
      echo "请输入 1 到 65535 之间的 TCP 端口。" >&2
      continue
    fi
    if port_in_use "$port_value"; then
      echo "端口 $port_value 已被其他程序占用，请换一个端口：" >&2
      ss -ltnp "sport = :$port_value" >&2 2>/dev/null || true
      continue
    fi
    printf -v "$target" '%s' "$port_value"
    return
  done
}

ask_proxy_base_url() {
  local raw scheme host
  while true; do
    ask raw "买家访问的地址（反向代理上配置的域名，如 https://shop.example.com）"
    case "$raw" in
      http://*) scheme="http" ;;
      *) scheme="https" ;;
    esac
    host="$(normalize_host "$raw")" || continue
    app_base_url="$scheme://$host"
    if [ "$scheme" = http ]; then
      echo "注意：使用 http:// 时管理员密码、取卡密码和卡密都会明文传输，正式收款请在反向代理上启用 HTTPS。" >&2
    fi
    return
  done
}

ask_admin_username() {
  while true; do
    ask admin_username "管理员账号（建议使用邮箱）"
    if [[ "$admin_username" =~ ^[^[:space:][:cntrl:]]{3,64}$ ]]; then
      return
    fi
    echo "管理员账号需为 3-64 位，且不能包含空格。" >&2
  done
}

ask_admin_password() {
  local first second weak
  while true; do
    IFS= read -r -s -p "管理员密码（至少 12 位）: " first || input_closed
    echo >&2
    IFS= read -r -s -p "再次输入管理员密码: " second || input_closed
    echo >&2
    if [ "$first" != "$second" ]; then
      echo "两次输入的密码不一致，请重试。" >&2
      continue
    fi
    if [ "${#first}" -lt 12 ]; then
      echo "密码短于 12 位，请重试。" >&2
      continue
    fi
    if [ "$first" = "$admin_username" ]; then
      echo "密码不能与管理员账号相同，请重试。" >&2
      continue
    fi
    for weak in "${WEAK_ADMIN_PASSWORDS[@]}"; do
      if [ "$first" = "$weak" ]; then
        echo "不能使用示例密码，请重试。" >&2
        continue 2
      fi
    done
    admin_password="$first"
    return
  done
}

# Let's Encrypt 拒绝 example.com 之类的保留域名邮箱；不像邮箱的账号就不写，证书照样能申请。
acme_email_for() {
  local value="${1,,}"
  if [[ "$value" =~ ^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$ ]] \
    && ! [[ "$value" =~ @(.+\.)?(example\.(com|net|org)|localhost|invalid|test)$ ]]; then
    printf '%s' "$1"
  fi
}

backup_existing_config() {
  local suffix file
  suffix="bak-$(date +%Y%m%d-%H%M%S)"
  for file in .env Caddyfile docker-compose.override.yml; do
    if [ -f "$file" ]; then
      cp -p "$file" "$file.$suffix"
      echo "已备份 $file → $file.$suffix"
    fi
  done
}

# ---------------------------------------------------------------- 预检

require_command docker
if ! docker compose version >/dev/null 2>&1; then
  echo "需要 Docker Compose 插件。请先安装 Docker，然后重新运行此脚本。" >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "无法连接 Docker 服务。请确认 Docker 已启动（systemctl start docker），" >&2
  echo "并使用 root 或 docker 组用户运行此脚本。" >&2
  exit 1
fi

echo "Card Issuance 交互式部署"
echo

existing_secret=""
if [ -f .env ]; then
  echo "检测到已有部署配置（.env）。重新配置会覆盖 .env、Caddyfile 和 docker-compose.override.yml，"
  echo "旧文件会先备份；数据库、商品和订单数据不受影响。只想升级程序请改用 ./scripts/update.sh。"
  if ! ask_yes_no "继续重新配置吗？" n; then
    echo "已取消，未做任何修改。"
    exit 0
  fi
  # 沿用原会话密钥：买家浏览器里已授权查看的订单不会因重新配置而失效。
  existing_secret="$(get_env APP_SECRET)"
  if [ "${#existing_secret}" -lt 32 ]; then
    existing_secret=""
  fi
fi

echo "部署模式："
echo "  1) HTTPS，使用内置 Caddy 自动申请证书（推荐，需要域名，占用 80/443 端口）"
echo "  2) HTTP 直连端口（仅用于测试，所有数据明文传输）"
echo "  3) 本机已有 Nginx / 宝塔等反向代理（应用只监听本机端口，由反向代理对外提供 HTTPS）"
while true; do
  ask mode "请选择模式" "1"
  case "$mode" in
    1|https|HTTPS) mode="https"; break ;;
    2|http|HTTP) mode="http"; break ;;
    3|proxy) mode="proxy"; break ;;
    *) echo "请输入 1、2 或 3。" >&2 ;;
  esac
done

if [ "$mode" = "https" ]; then
  require_command curl "HTTPS 部署需要 curl。请先安装 curl 后重新运行此脚本。"
  check_https_ports
  ask_https_domain
  app_base_url="https://$domain"
  app_port=""
elif [ "$mode" = "http" ]; then
  while true; do
    ask public_host_raw "公开域名或服务器 IP" "$(detect_public_ip)"
    if public_host="$(normalize_host "$public_host_raw")"; then
      break
    fi
  done
  ask_port app_port "公开 HTTP 端口"
  app_base_url="http://$public_host:$app_port"
else
  ask_proxy_base_url
  ask_port app_port "应用监听的本机端口（反向代理转发到 http://127.0.0.1:端口）"
fi

ask_admin_username
ask_admin_password

# 后台路径随机生成（隐藏后台、减少扫描），部署完成时显示。
admin_path="/admin-$(random_suffix)"
app_secret="${existing_secret:-$(generate_secret)}"

echo
echo "即将使用以下配置部署："
case "$mode" in
  https) echo "  模式：HTTPS（内置 Caddy）" ;;
  http) echo "  模式：HTTP 直连端口 $app_port" ;;
  proxy) echo "  模式：反向代理 → http://127.0.0.1:$app_port" ;;
esac
echo "  前台地址：$app_base_url/"
echo "  后台地址：$app_base_url$admin_path/login"
echo "  管理员账号：$admin_username"
if ! ask_yes_no "确认开始部署吗？" y; then
  echo "已取消，未做任何修改。"
  exit 0
fi

backup_existing_config

if [ "$mode" = "https" ]; then
  acme_email="$(acme_email_for "$admin_username")"
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
elif [ "$mode" = "http" ]; then
  rm -f Caddyfile
  cat > docker-compose.override.yml <<EOF_COMPOSE
services:
  card-issuance:
    ports:
      - "0.0.0.0:$app_port:8080"
EOF_COMPOSE
else
  rm -f Caddyfile
  # 只绑定本机：外部无法绕过反向代理直连并伪造 X-Forwarded-For。
  cat > docker-compose.override.yml <<EOF_COMPOSE
services:
  card-issuance:
    ports:
      - "127.0.0.1:$app_port:8080"
EOF_COMPOSE
fi

case "$mode" in
  http) trusted_proxy_count=0 ;;
  *) trusted_proxy_count=1 ;;
esac

cat > .env <<EOF_ENV
APP_NAME=$(dotenv_literal "Card Issuance")
APP_SECRET=$app_secret
APP_BASE_URL=$app_base_url
DATABASE_PATH=/data/card_issuance.sqlite3
ADMIN_PATH=$admin_path
ADMIN_USERNAME=$(dotenv_literal "$admin_username")
ADMIN_PASSWORD=$(dotenv_literal "$admin_password")
ENABLE_MOCK_PAYMENTS=0
ORDER_EXPIRE_MINUTES=15
TRUSTED_PROXY_COUNT=$trusted_proxy_count
CONTAINER_DNS_PRIMARY=1.1.1.1
CONTAINER_DNS_SECONDARY=8.8.8.8
EOF_ENV
chmod 600 .env

mkdir -p data backups
# 数据库和备份里有卡密、买家邮箱和 IP：只允许部署用户（及容器内 root）读取。
chmod 700 data backups

if ! docker compose up -d --build --remove-orphans; then
  echo "容器构建或启动失败，请查看上方 Docker 输出（常见原因：网络无法拉取镜像或依赖、端口被占用）。" >&2
  echo "配置已写入 .env，排查后可运行 ./scripts/deploy.sh 重试。" >&2
  exit 1
fi

bash ./scripts/check-ready.sh "$app_base_url"

echo
echo "部署完成。"
echo "前台：$app_base_url/"
echo "管理后台：$app_base_url$admin_path/login"
echo "管理员账号：$admin_username"
echo "请保存后台地址（路径是随机生成的，也可在 .env 的 ADMIN_PATH 中查看）。"
echo "站点名称、订单过期时间等可在 .env 中修改（APP_NAME、ORDER_EXPIRE_MINUTES），改完执行 docker compose up -d 生效。"
case "$mode" in
  https)
    echo
    echo "请确认 VPS 防火墙或云厂商安全组已开放 80 和 443 端口。"
    echo "访问时无需添加 :8080，正确地址为 $app_base_url/"
    echo "若 HTTPS 无法访问，请检查：docker compose logs -f caddy"
    ;;
  http)
    echo
    echo "HTTP 直连模式使用端口 $app_port。"
    echo "请在 VPS 防火墙或安全组中开放 TCP $app_port，然后访问 $app_base_url/"
    echo "HTTP 下所有数据明文传输，正式收款前请改用 HTTPS 模式。"
    ;;
  proxy)
    echo
    echo "请在反向代理上把 ${app_base_url#*://} 转发到 http://127.0.0.1:$app_port，并传递以下请求头（Nginx 示例）："
    echo "    proxy_set_header Host \$host;"
    echo "    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;"
    echo "    proxy_set_header X-Forwarded-Proto \$scheme;"
    ;;
esac
