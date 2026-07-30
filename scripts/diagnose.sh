#!/usr/bin/env bash
set -u

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

echo "开始诊断 Card Issuance..."

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
container_dns_status=125
app_direct_status=125
tls_status=125
insecure_status=125

section "部署配置"
echo "APP_BASE_URL=${app_base_url:-未设置}"
if [ -n "$admin_path" ]; then
  echo "ADMIN_PATH 已配置"
else
  echo "ADMIN_PATH 未设置"
fi
if [ -f Caddyfile ]; then
  echo "模式：使用 Caddy 的 HTTPS"
else
  echo "模式：直接 HTTP 或外部反向代理"
fi

section "Docker 状态"
run docker compose ps

section "应用日志"
run docker compose logs --tail=80 card-issuance

if [ -f Caddyfile ]; then
  section "Caddy 日志"
  run docker compose logs --tail=120 caddy
fi

section "容器 DNS"
run docker compose exec -T card-issuance cat /etc/resolv.conf
if [ -f Caddyfile ]; then
  run docker compose exec -T caddy cat /etc/resolv.conf
  if docker compose exec -T caddy nslookup acme-v02.api.letsencrypt.org; then
    container_dns_status=0
  else
    container_dns_status=$?
  fi
else
  echo "未使用项目 Caddy；跳过 Caddy 容器 DNS 解析检查。"
fi

section "监听端口"
if command -v ss >/dev/null 2>&1; then
  run sh -c "ss -lntp | grep -E ':(80|443|8080)\\b'"
else
  echo "未安装 ss。"
fi

section "应用直连"
if [ -f Caddyfile ]; then
  echo "从 Caddy 容器直连应用：http://card-issuance:8080/"
  if docker compose exec -T caddy wget -q --spider -T 10 http://card-issuance:8080/; then
    app_direct_status=0
  else
    app_direct_status=$?
  fi
fi

section "HTTP/TLS 检查"
if command -v curl >/dev/null 2>&1; then
  if [[ "$app_base_url" == https://* ]]; then
    domain="${app_base_url#https://}"
    domain="${domain%%/*}"
    echo "使用正常证书校验检查本机 Caddy：$domain"
    if curl --silent --show-error --head --max-time 10 \
      --resolve "$domain:443:127.0.0.1" "https://$domain/"; then
      tls_status=0
    else
      tls_status=$?
    fi

    echo "使用 --insecure 仅对照 HTTPS 连通性：$domain"
    if curl --silent --show-error --head --insecure --max-time 10 \
      --resolve "$domain:443:127.0.0.1" "https://$domain/"; then
      insecure_status=0
    else
      insecure_status=$?
    fi
  elif [ -n "$app_base_url" ]; then
    echo "检查配置的 URL：$app_base_url/"
    if curl --silent --show-error --fail --head --max-time 10 "$app_base_url/"; then
      app_direct_status=0
    else
      app_direct_status=$?
    fi
  else
    echo "缺少 APP_BASE_URL；跳过 URL 检查。"
  fi
else
  echo "未安装 curl。"
fi

section "DNS 提示"
if [[ "$app_base_url" == https://* || "$app_base_url" == http://* ]]; then
  host="${app_base_url#http://}"
  host="${host#https://}"
  host="${host%%/*}"
  host="${host%%:*}"
  if command -v getent >/dev/null 2>&1; then
    run getent hosts "$host"
  else
    echo "未安装 getent。"
  fi
fi

section "分类总结"
diagnosis_found=false

if [ "$container_dns_status" -eq 0 ]; then
  echo "容器 DNS：正常"
elif [ "$container_dns_status" -eq 125 ]; then
  echo "容器 DNS：未检查"
else
  echo "容器 DNS：异常"
  echo "诊断结论：DNS 故障，Caddy 容器无法解析证书服务域名。"
  diagnosis_found=true
fi

if [ "$app_direct_status" -eq 0 ]; then
  echo "应用直连：正常"
elif [ "$app_direct_status" -eq 125 ]; then
  echo "应用直连：未检查"
else
  echo "应用直连：异常"
  echo "诊断结论：应用故障，Caddy 容器无法直连 card-issuance:8080。"
  diagnosis_found=true
fi

if [ "$tls_status" -eq 0 ]; then
  echo "证书校验：正常"
elif [ "$tls_status" -eq 125 ]; then
  echo "证书校验：未检查"
elif [ "$insecure_status" -eq 0 ]; then
  echo "证书校验：异常"
  echo "诊断结论：证书故障，HTTPS 可连通但正常 TLS 校验失败。"
  diagnosis_found=true
else
  echo "证书校验：无法判断"
fi

if [ "$insecure_status" -eq 0 ]; then
  echo "-k 连通性对照：正常"
elif [ "$insecure_status" -eq 125 ]; then
  echo "-k 连通性对照：未检查"
else
  echo "-k 连通性对照：异常"
fi

if [ "$tls_status" -eq 0 ] || [ "$insecure_status" -eq 0 ]; then
  echo "Caddy/端口：正常"
elif [ "$tls_status" -eq 125 ] && [ "$insecure_status" -eq 125 ]; then
  echo "Caddy/端口：无法判断"
else
  echo "Caddy/端口：异常"
  echo "诊断结论：Caddy/端口故障，本机 443 的 HTTPS 连通性检查失败。"
  diagnosis_found=true
fi

if [ "$diagnosis_found" = false ]; then
  echo "诊断结论：上述检查未发现 DNS、应用、证书或 Caddy/端口故障。"
fi

echo
echo "使用 HTTPS 模式时，浏览器地址应为 https://domain/，不要带 :8080。"
echo "使用直接 HTTP 模式时，浏览器地址应为 http://domain:port/，且对应 TCP 端口必须开放。"
