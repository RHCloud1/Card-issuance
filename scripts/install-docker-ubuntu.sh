#!/usr/bin/env bash
set -euo pipefail

echo "开始安装 Docker..."

if ! command -v apt-get >/dev/null 2>&1; then
  echo "此安装脚本仅支持带有 apt-get 的 Ubuntu/Debian 系统。" >&2
  exit 1
fi

run_as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "此命令需要 root 权限，但未安装 sudo。请以 root 身份重新运行。" >&2
    exit 1
  fi
}

docker_repository_for_distribution() {
  local distribution="$1"
  local codename="$2"
  case "$distribution" in
    ubuntu|debian) ;;
    *)
      echo "不支持的系统：${distribution:-unknown}。仅支持 Ubuntu/Debian。" >&2
      return 1
      ;;
  esac
  if ! [[ "$codename" =~ ^[a-z][a-z0-9-]*$ ]]; then
    echo "无法确认系统版本代号，已停止安装 Docker。" >&2
    return 1
  fi
  printf 'https://download.docker.com/linux/%s' "$distribution"
}

# Select the matching official repository before changing the host.
. /etc/os-release
docker_repository_url="$(docker_repository_for_distribution "${ID:-}" "${VERSION_CODENAME:-}")"

run_as_root apt-get update
run_as_root apt-get install -y ca-certificates curl gnupg git

run_as_root install -m 0755 -d /etc/apt/keyrings
tmp_key="$(mktemp)"
curl -fsSL "$docker_repository_url/gpg" -o "$tmp_key"
run_as_root gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg "$tmp_key"
rm -f "$tmp_key"
run_as_root chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] $docker_repository_url ${VERSION_CODENAME} stable" \
  | run_as_root tee /etc/apt/sources.list.d/docker.list >/dev/null

run_as_root apt-get update
run_as_root apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

run_as_root systemctl enable --now docker
docker --version
docker compose version
