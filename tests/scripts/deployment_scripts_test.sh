#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fixture=""
setup_status=0
diagnose_status=0

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

assert_contains() {
  local file="$1"
  local expected="$2"
  grep -F -- "$expected" "$file" >/dev/null || fail "expected $file to contain: $expected"
}

assert_not_contains() {
  local file="$1"
  local unexpected="$2"
  if grep -F -- "$unexpected" "$file" >/dev/null; then
    fail "expected $file not to contain: $unexpected"
  fi
}

assert_success() {
  [ "$setup_status" -eq 0 ] || fail "expected setup to succeed, got exit status $setup_status"
}

assert_failure() {
  [ "$setup_status" -ne 0 ] || fail "expected setup to fail"
}

assert_line_count() {
  local file="$1"
  local expected="$2"
  local count="$3"
  local actual
  actual="$(grep -F -c -- "$expected" "$file" || true)"
  [ "$actual" -eq "$count" ] || fail "expected $file to contain $count lines matching: $expected (got $actual)"
}

cleanup_fixture() {
  if [ -n "$fixture" ] && [ -d "$fixture" ]; then
    rm -rf "$fixture"
  fi
  fixture=""
}

make_fixture() {
  cleanup_fixture
  fixture="$(mktemp -d)"
  mkdir -p "$fixture/scripts" "$fixture/fake-bin"
  cp "$repo_root/scripts/setup.sh" "$fixture/scripts/setup.sh"
  cp "$repo_root/scripts/diagnose.sh" "$fixture/scripts/diagnose.sh"
  cp "$repo_root/docker-compose.yml" "$fixture/docker-compose.yml"

  cat > "$fixture/fake-bin/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"

if [ "${1:-}" = "ps" ]; then
  if [ "${FAKE_DOCKER_CADDY:-0}" = "1" ]; then
    requested_publish=""
    for argument in "$@"; do
      case "$argument" in
        publish=*) requested_publish="${argument#publish=}" ;;
      esac
    done
    requested_publish="${requested_publish%/tcp}"

    if [ -z "$requested_publish" ]; then
      printf '%s\n' 'card-issuance-caddy'
    else
      for exposed_port in ${FAKE_DOCKER_CADDY_EXPOSED_PORTS:-}; do
        if [ "$exposed_port" = "$requested_publish" ]; then
          printf '%s\n' 'card-issuance-caddy'
          break
        fi
      done
    fi
  fi
  exit 0
fi

if [ "${1:-}" = "inspect" ]; then
  if [ "${FAKE_DOCKER_INSPECT_RESULT:-success}" != "success" ]; then
    exit 1
  fi

  requested_container_port=""
  case "$*" in
    *'"80/tcp"'*) requested_container_port="80" ;;
    *'"443/tcp"'*) requested_container_port="443" ;;
  esac

  if [ "${FAKE_DOCKER_CADDY:-0}" = "1" ]; then
    for binding in ${FAKE_DOCKER_CADDY_BINDINGS:-}; do
      IFS='|' read -r container_port host_ip host_port <<< "$binding"
      if [ "$container_port" = "$requested_container_port" ]; then
        printf '%s|%s\n' "$host_ip" "$host_port"
      fi
    done
  fi
  exit 0
fi

if [ "${1:-}" = "port" ] && [ "${2:-}" = "card-issuance-caddy" ]; then
  requested_container_port="${3:-}"
  requested_container_port="${requested_container_port%/tcp}"
  if [ "${FAKE_DOCKER_CADDY:-0}" = "1" ]; then
    for binding in ${FAKE_DOCKER_CADDY_BINDINGS:-}; do
      IFS='|' read -r container_port host_ip host_port <<< "$binding"
      if [ "$container_port" = "$requested_container_port" ]; then
        case "$host_ip" in
          *:*) printf '[%s]:%s\n' "$host_ip" "$host_port" ;;
          *) printf '%s:%s\n' "$host_ip" "$host_port" ;;
        esac
      fi
    done
  fi
  exit 0
fi

if [ "${1:-}" = "compose" ] && [ "${2:-}" = "version" ]; then
  exit 0
fi

if [ "${1:-}" = "compose" ] && [ "${2:-}" = "exec" ]; then
  case "${5:-}" in
    cat)
      printf '%s\n' 'nameserver 1.1.1.1'
      ;;
    nslookup)
      nslookup "${6:-}"
      ;;
    wget)
      if [ "${FAKE_APP_RESULT:-success}" != "success" ]; then
        exit 1
      fi
      ;;
  esac
  exit 0
fi

exit 0
EOF

  cat > "$fixture/fake-bin/ss" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
filter_port=""
arguments="$*"
if [[ "$arguments" == *":80"* && "$arguments" == *":443"* ]]; then
  filter_port=""
elif [[ "$arguments" == *":80"* ]]; then
  filter_port="80"
elif [[ "$arguments" == *":443"* ]]; then
  filter_port="443"
fi

for endpoint in ${FAKE_SS_ENDPOINTS:-}; do
  endpoint_port="${endpoint##*:}"
  if [ -z "$filter_port" ] || [ "$endpoint_port" = "$filter_port" ]; then
    printf 'LISTEN 0 4096 %s 0.0.0.0:*\n' "$endpoint"
  fi
done
EOF

  cat > "$fixture/fake-bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_CURL_LOG"

result="${FAKE_CURL_RESULT:-success}"
case " $* " in
  *" --insecure "*|*" -k "*) result="${FAKE_CURL_INSECURE_RESULT:-$result}" ;;
  *) result="${FAKE_CURL_TLS_RESULT:-$result}" ;;
esac

if [ "$result" = "success" ]; then
  exit 0
fi
exit 1
EOF

  cat > "$fixture/fake-bin/nslookup" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_NSLOOKUP_LOG"
if [ "${FAKE_DNS_RESULT:-success}" = "success" ]; then
  printf '%s\n' 'Address: 192.0.2.1'
  exit 0
fi
exit 1
EOF

  cat > "$fixture/fake-bin/sleep" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

  chmod +x "$fixture/fake-bin/docker" "$fixture/fake-bin/ss" "$fixture/fake-bin/curl" \
    "$fixture/fake-bin/nslookup" "$fixture/fake-bin/sleep"
  export FAKE_DOCKER_LOG="$fixture/docker.log"
  export FAKE_CURL_LOG="$fixture/curl.log"
  export FAKE_NSLOOKUP_LOG="$fixture/nslookup.log"
  : > "$FAKE_DOCKER_LOG"
  : > "$FAKE_CURL_LOG"
  : > "$FAKE_NSLOOKUP_LOG"
  export FAKE_DOCKER_CADDY=0
  export FAKE_DOCKER_INSPECT_RESULT=success
  export FAKE_DOCKER_CADDY_EXPOSED_PORTS='80 443'
  export FAKE_DOCKER_CADDY_BINDINGS='80|0.0.0.0|80 443|0.0.0.0|443'
  export FAKE_SS_ENDPOINTS=""
  export FAKE_CURL_RESULT=success
  export FAKE_CURL_TLS_RESULT=success
  export FAKE_CURL_INSECURE_RESULT=success
  export FAKE_DNS_RESULT=success
  export FAKE_APP_RESULT=success
}

run_https_setup() {
  local output="$fixture/setup.out"
  set +e
  printf '%s\n' \
    '1' \
    '' \
    '' \
    '' \
    'admin@example.com' \
    'correct-horse-battery-staple' \
    'correct-horse-battery-staple' \
    'card.example.test' \
    '' \
    | (cd "$fixture" && PATH="$fixture/fake-bin:$PATH" bash ./scripts/setup.sh) > "$output" 2>&1
  setup_status=$?
  set -e
}

run_https_setup_without_curl() {
  local output="$fixture/setup.out"
  local no_curl_bin="$fixture/no-curl-bin"
  mkdir -p "$no_curl_bin"

  cat > "$no_curl_bin/dirname" <<'EOF'
#!/bin/sh
path="${1%/}"
case "$path" in
  */*) printf '%s\n' "${path%/*}" ;;
  *) printf '%s\n' '.' ;;
esac
EOF

  cat > "$no_curl_bin/docker" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
if [ "${1:-}" = "compose" ] && [ "${2:-}" = "version" ]; then
  exit 0
fi
exit 0
EOF

  chmod +x "$no_curl_bin/dirname" "$no_curl_bin/docker"

  set +e
  printf '%s\n' '1' \
    | (cd "$fixture" && PATH="$no_curl_bin" "$BASH" ./scripts/setup.sh) > "$output" 2>&1
  setup_status=$?
  set -e
}

prepare_https_diagnose_fixture() {
  cat > "$fixture/.env" <<'EOF'
APP_BASE_URL=https://card.example.test
ADMIN_PATH=/admin-test
EOF
  cat > "$fixture/Caddyfile" <<'EOF'
card.example.test {
    reverse_proxy card-issuance:8080
}
EOF
}

run_https_diagnose() {
  local name="$1"
  local output="$fixture/diagnose-$name.out"
  set +e
  (cd "$fixture" && PATH="$fixture/fake-bin:$PATH" bash ./scripts/diagnose.sh) > "$output" 2>&1
  diagnose_status=$?
  set -e
}

test_https_setup_generates_dns_configuration() {
  make_fixture
  run_https_setup

  assert_success
  assert_contains "$fixture/docker-compose.override.yml" '${CONTAINER_DNS_PRIMARY:-1.1.1.1}'
  assert_contains "$fixture/docker-compose.override.yml" '${CONTAINER_DNS_SECONDARY:-8.8.8.8}'
  assert_contains "$fixture/.env" 'CONTAINER_DNS_PRIMARY=1.1.1.1'
  assert_contains "$fixture/.env" 'CONTAINER_DNS_SECONDARY=8.8.8.8'
  assert_contains "$repo_root/docker-compose.yml" '${CONTAINER_DNS_PRIMARY:-1.1.1.1}'
}

test_https_setup_rejects_third_party_port_443() {
  make_fixture
  export FAKE_SS_ENDPOINTS='0.0.0.0:443'
  run_https_setup

  assert_failure
  assert_contains "$fixture/setup.out" '端口 443 已被占用'
  assert_not_contains "$FAKE_DOCKER_LOG" 'compose up'
}

test_https_setup_allows_its_own_caddy_to_use_80_and_443() {
  make_fixture
  export FAKE_DOCKER_CADDY=1
  export FAKE_DOCKER_CADDY_BINDINGS='80|0.0.0.0|80 80|::|80 443|0.0.0.0|443 443|::|443'
  export FAKE_SS_ENDPOINTS='0.0.0.0:80 [::]:80 0.0.0.0:443 [::]:443'
  run_https_setup

  assert_success
  assert_contains "$fixture/docker-compose.override.yml" 'container_name: card-issuance-caddy'
  assert_contains "$FAKE_DOCKER_LOG" 'compose up -d --build'
  assert_contains "$FAKE_DOCKER_LOG" 'inspect --format'
  assert_contains "$FAKE_DOCKER_LOG" '.HostIp .HostPort'
  assert_contains "$FAKE_DOCKER_LOG" '"80/tcp"'
  assert_contains "$FAKE_DOCKER_LOG" '"443/tcp"'
}

test_https_setup_rejects_exposed_port_without_host_mapping() {
  make_fixture
  export FAKE_DOCKER_CADDY=1
  export FAKE_DOCKER_CADDY_EXPOSED_PORTS='443'
  export FAKE_DOCKER_CADDY_BINDINGS=''
  export FAKE_SS_ENDPOINTS='0.0.0.0:443'
  run_https_setup

  assert_failure
  assert_contains "$fixture/setup.out" '端口 443 已被占用'
  assert_not_contains "$FAKE_DOCKER_LOG" 'compose up'
}

test_https_setup_rejects_different_host_port_mapping() {
  make_fixture
  export FAKE_DOCKER_CADDY=1
  export FAKE_DOCKER_CADDY_EXPOSED_PORTS='80'
  export FAKE_DOCKER_CADDY_BINDINGS='80|0.0.0.0|8080'
  export FAKE_SS_ENDPOINTS='0.0.0.0:80'
  run_https_setup

  assert_failure
  assert_contains "$fixture/setup.out" '端口 80 已被占用'
  assert_not_contains "$FAKE_DOCKER_LOG" 'compose up'
}

test_https_setup_rejects_unowned_port_when_caddy_publishes_only_other_port() {
  make_fixture
  export FAKE_DOCKER_CADDY=1
  export FAKE_DOCKER_CADDY_BINDINGS='80|0.0.0.0|80'
  export FAKE_SS_ENDPOINTS='0.0.0.0:80 0.0.0.0:443'
  run_https_setup

  assert_failure
  assert_contains "$fixture/setup.out" '端口 443 已被占用'
  assert_not_contains "$FAKE_DOCKER_LOG" 'compose up'
}

test_https_setup_rejects_ipv4_listener_not_owned_by_caddy() {
  make_fixture
  export FAKE_DOCKER_CADDY=1
  export FAKE_DOCKER_CADDY_BINDINGS='443|192.0.2.10|443'
  export FAKE_SS_ENDPOINTS='192.0.2.10:443 192.0.2.20:443'
  run_https_setup

  assert_failure
  assert_contains "$fixture/setup.out" '端口 443 已被占用'
  assert_not_contains "$FAKE_DOCKER_LOG" 'compose up'
}

test_https_setup_rejects_ipv6_listener_not_owned_by_caddy() {
  make_fixture
  export FAKE_DOCKER_CADDY=1
  export FAKE_DOCKER_CADDY_BINDINGS='443|2001:db8::10|443'
  export FAKE_SS_ENDPOINTS='[2001:db8::10]:443 [2001:db8::20]:443'
  run_https_setup

  assert_failure
  assert_contains "$fixture/setup.out" '端口 443 已被占用'
  assert_not_contains "$FAKE_DOCKER_LOG" 'compose up'
}

test_https_setup_rejects_port_when_caddy_bindings_cannot_be_inspected() {
  make_fixture
  export FAKE_DOCKER_CADDY=1
  export FAKE_DOCKER_INSPECT_RESULT=failure
  export FAKE_SS_ENDPOINTS='0.0.0.0:443'
  run_https_setup

  assert_failure
  assert_contains "$fixture/setup.out" '端口 443 已被占用'
  assert_not_contains "$FAKE_DOCKER_LOG" 'compose up'
}

test_https_setup_reports_ready_after_https_check_succeeds() {
  make_fixture
  export FAKE_CURL_RESULT=success
  run_https_setup

  assert_success
  assert_contains "$fixture/setup.out" '部署完成'
  assert_line_count "$FAKE_CURL_LOG" '--silent --show-error --fail --head --max-time 5 --resolve card.example.test:443:127.0.0.1 https://card.example.test/' 1
  assert_not_contains "$FAKE_CURL_LOG" '--insecure'
  assert_not_contains "$FAKE_CURL_LOG" '-k'
}

test_https_setup_reports_diagnostic_command_when_https_check_times_out() {
  make_fixture
  export FAKE_CURL_RESULT=failure
  export FAKE_CURL_TLS_RESULT=failure
  run_https_setup

  assert_failure
  assert_contains "$fixture/setup.out" 'HTTPS 尚未就绪'
  assert_contains "$fixture/setup.out" './scripts/diagnose.sh'
}

test_https_setup_requires_curl_before_build() {
  make_fixture
  run_https_setup_without_curl

  assert_failure
  assert_contains "$fixture/setup.out" '缺少必需命令：curl'
  assert_contains "$fixture/setup.out" '请先安装 curl'
  assert_not_contains "$FAKE_DOCKER_LOG" 'compose up'
}

test_diagnose_reports_successful_matrix() {
  make_fixture
  prepare_https_diagnose_fixture
  run_https_diagnose success

  [ "$diagnose_status" -eq 0 ] || fail "expected diagnose success matrix to exit zero"
  assert_contains "$fixture/diagnose-success.out" '容器 DNS：正常'
  assert_contains "$fixture/diagnose-success.out" '应用直连：正常'
  assert_contains "$fixture/diagnose-success.out" '证书校验：正常'
  assert_contains "$fixture/diagnose-success.out" '-k 连通性对照：正常'
  assert_contains "$fixture/diagnose-success.out" 'Caddy/端口：正常'
  assert_contains "$FAKE_DOCKER_LOG" 'compose exec -T caddy nslookup acme-v02.api.letsencrypt.org'
  assert_contains "$FAKE_DOCKER_LOG" 'compose exec -T caddy wget -q --spider -T 10 http://card-issuance:8080/'
  assert_contains "$FAKE_NSLOOKUP_LOG" 'acme-v02.api.letsencrypt.org'
  assert_line_count "$FAKE_CURL_LOG" '--resolve card.example.test:443:127.0.0.1' 2
  assert_line_count "$FAKE_CURL_LOG" '--insecure' 1
  assert_not_contains "$FAKE_CURL_LOG" '--fail'
}

test_diagnose_reports_container_dns_failure() {
  make_fixture
  prepare_https_diagnose_fixture
  export FAKE_DNS_RESULT=failure
  run_https_diagnose dns-failure

  assert_contains "$fixture/diagnose-dns-failure.out" '容器 DNS：异常'
  assert_contains "$fixture/diagnose-dns-failure.out" 'DNS 故障'
}

test_diagnose_reports_application_failure() {
  make_fixture
  prepare_https_diagnose_fixture
  export FAKE_APP_RESULT=failure
  run_https_diagnose app-failure

  assert_contains "$fixture/diagnose-app-failure.out" '应用直连：异常'
  assert_contains "$fixture/diagnose-app-failure.out" '应用故障'
}

test_diagnose_reports_certificate_failure() {
  make_fixture
  prepare_https_diagnose_fixture
  export FAKE_CURL_TLS_RESULT=failure
  export FAKE_CURL_INSECURE_RESULT=success
  run_https_diagnose certificate-failure

  assert_contains "$fixture/diagnose-certificate-failure.out" '证书校验：异常'
  assert_contains "$fixture/diagnose-certificate-failure.out" '证书故障'
  assert_contains "$fixture/diagnose-certificate-failure.out" 'Caddy/端口：正常'
}

test_diagnose_reports_caddy_or_port_failure() {
  make_fixture
  prepare_https_diagnose_fixture
  export FAKE_CURL_TLS_RESULT=failure
  export FAKE_CURL_INSECURE_RESULT=failure
  run_https_diagnose caddy-failure

  assert_contains "$fixture/diagnose-caddy-failure.out" '证书校验：无法判断'
  assert_contains "$fixture/diagnose-caddy-failure.out" 'Caddy/端口：异常'
  assert_contains "$fixture/diagnose-caddy-failure.out" 'Caddy/端口故障'
}

test_diagnose_remains_read_only() {
  make_fixture
  prepare_https_diagnose_fixture
  cp "$fixture/.env" "$fixture/env.before"
  cp "$fixture/Caddyfile" "$fixture/caddyfile.before"
  run_https_diagnose read-only

  cmp -s "$fixture/env.before" "$fixture/.env" || fail 'diagnose changed .env'
  cmp -s "$fixture/caddyfile.before" "$fixture/Caddyfile" || fail 'diagnose changed Caddyfile'
  assert_not_contains "$FAKE_DOCKER_LOG" 'compose up'
  assert_not_contains "$FAKE_DOCKER_LOG" 'compose down'
  assert_not_contains "$FAKE_DOCKER_LOG" 'compose stop'
  assert_not_contains "$FAKE_DOCKER_LOG" 'compose restart'
  assert_not_contains "$FAKE_DOCKER_LOG" 'compose kill'
}

test_maintenance_scripts_include_chinese_output_and_dns_diagnostics() {
  assert_contains "$repo_root/scripts/install.sh" '开始安装 Card Issuance'
  assert_contains "$repo_root/scripts/install-docker-ubuntu.sh" '开始安装 Docker'
  assert_contains "$repo_root/scripts/deploy.sh" '开始部署 Card Issuance'
  assert_contains "$repo_root/scripts/update.sh" '开始更新 Card Issuance'
  assert_contains "$repo_root/scripts/diagnose.sh" '开始诊断 Card Issuance'
  assert_contains "$repo_root/scripts/diagnose.sh" '容器 DNS'
  assert_contains "$repo_root/scripts/diagnose.sh" 'docker compose exec -T card-issuance cat /etc/resolv.conf'
  assert_contains "$repo_root/scripts/diagnose.sh" 'docker compose exec -T caddy cat /etc/resolv.conf'
  assert_contains "$repo_root/scripts/diagnose.sh" 'docker compose exec -T caddy nslookup acme-v02.api.letsencrypt.org'
  assert_contains "$repo_root/README.md" '脚本结束时输出的 `前台`'
  assert_contains "$repo_root/README.md" '脚本结束时输出的 `管理后台`'
  assert_not_contains "$repo_root/README.md" '脚本结束时输出的 `Frontend`'
  assert_not_contains "$repo_root/README.md" '脚本结束时输出的 `Admin`'
}

trap cleanup_fixture EXIT

tests=(
  test_https_setup_generates_dns_configuration
  test_https_setup_rejects_third_party_port_443
  test_https_setup_allows_its_own_caddy_to_use_80_and_443
  test_https_setup_rejects_exposed_port_without_host_mapping
  test_https_setup_rejects_different_host_port_mapping
  test_https_setup_rejects_unowned_port_when_caddy_publishes_only_other_port
  test_https_setup_rejects_ipv4_listener_not_owned_by_caddy
  test_https_setup_rejects_ipv6_listener_not_owned_by_caddy
  test_https_setup_rejects_port_when_caddy_bindings_cannot_be_inspected
  test_https_setup_reports_ready_after_https_check_succeeds
  test_https_setup_reports_diagnostic_command_when_https_check_times_out
  test_https_setup_requires_curl_before_build
  test_diagnose_reports_successful_matrix
  test_diagnose_reports_container_dns_failure
  test_diagnose_reports_application_failure
  test_diagnose_reports_certificate_failure
  test_diagnose_reports_caddy_or_port_failure
  test_diagnose_remains_read_only
  test_maintenance_scripts_include_chinese_output_and_dns_diagnostics
)

if [ "$#" -gt 0 ]; then
  tests=("$@")
fi

for test_name in "${tests[@]}"; do
  "$test_name"
done

printf 'PASS: deployment script regression harness\n'
