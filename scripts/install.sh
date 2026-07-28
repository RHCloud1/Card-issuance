#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/RHCloud1/Card-issuance.git}"
RAW_BASE="${RAW_BASE:-https://raw.githubusercontent.com/RHCloud1/Card-issuance/main}"

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

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer targets Ubuntu/Debian systems with apt-get." >&2
  exit 1
fi

run_as_root apt-get update
run_as_root apt-get install -y ca-certificates curl git

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Docker or Docker Compose is missing. Installing Docker..."
  tmp_installer="$(mktemp)"
  curl -fsSL "$RAW_BASE/scripts/install-docker-ubuntu.sh" -o "$tmp_installer"
  bash "$tmp_installer"
  rm -f "$tmp_installer"
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker is installed, but the current user cannot run 'docker compose'." >&2
  echo "Use a root shell, or add this user to the docker group and log in again." >&2
  exit 1
fi

if [ "$(id -u)" -eq 0 ]; then
  default_install_dir="/opt/card-issuance"
else
  default_install_dir="$HOME/card-issuance"
fi

install_dir="$(prompt "Install directory" "$default_install_dir")"

if [ -d "$install_dir/.git" ]; then
  echo "Existing repository found at $install_dir. Updating..."
  git -C "$install_dir" pull --ff-only
elif [ -e "$install_dir" ]; then
  echo "$install_dir exists but is not a Git repository. Choose another directory or remove it." >&2
  exit 1
else
  git clone "$REPO_URL" "$install_dir"
fi

cd "$install_dir"
chmod +x scripts/*.sh
./scripts/setup.sh
