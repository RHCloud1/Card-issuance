#!/usr/bin/env bash
set -euo pipefail

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer targets Ubuntu/Debian systems with apt-get." >&2
  exit 1
fi

run_as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "This command needs root privileges, but sudo is not installed. Re-run as root." >&2
    exit 1
  fi
}

run_as_root apt-get update
run_as_root apt-get install -y ca-certificates curl gnupg git

run_as_root install -m 0755 -d /etc/apt/keyrings
tmp_key="$(mktemp)"
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$tmp_key"
run_as_root gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg "$tmp_key"
rm -f "$tmp_key"
run_as_root chmod a+r /etc/apt/keyrings/docker.gpg

. /etc/os-release
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  | run_as_root tee /etc/apt/sources.list.d/docker.list >/dev/null

run_as_root apt-get update
run_as_root apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

run_as_root systemctl enable --now docker
docker --version
docker compose version
