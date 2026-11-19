#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/RHCloud1/Card-issuance.git}"
RAW_BASE="${RAW_BASE:-https://raw.githubusercontent.com/RHCloud1/Card-issuance/main}"

echo "开始安装 Card Issuance..."

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

if ! command -v apt-get >/dev/null 2>&1; then
  echo "此安装脚本仅支持带有 apt-get 的 Ubuntu/Debian 系统。" >&2
  exit 1
fi

run_as_root apt-get update
run_as_root apt-get install -y ca-certificates curl git

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "未检测到 Docker 或 Docker Compose。正在安装 Docker..."
  tmp_installer="$(mktemp)"
  curl -fsSL "$RAW_BASE/scripts/install-docker-ubuntu.sh" -o "$tmp_installer"
  bash "$tmp_installer"
  rm -f "$tmp_installer"
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker 已安装，但当前用户无法运行 'docker compose'。" >&2
  echo "请使用 root shell，或将此用户添加到 docker 组后重新登录。" >&2
  exit 1
fi

# 安装目录不再询问：默认 /opt/card-issuance（非 root 为 ~/card-issuance），需要时用 INSTALL_DIR 指定。
if [ -n "${INSTALL_DIR:-}" ]; then
  install_dir="$INSTALL_DIR"
elif [ "$(id -u)" -eq 0 ]; then
  install_dir="/opt/card-issuance"
else
  install_dir="$HOME/card-issuance"
fi
echo "安装目录：$install_dir"

if [ -d "$install_dir/.git" ]; then
  echo "在 $install_dir 发现已有仓库。正在更新..."
  git -C "$install_dir" pull --ff-only
elif [ -e "$install_dir" ]; then
  echo "$install_dir 已存在，但不是 Git 仓库。请用 INSTALL_DIR=其他目录 重新运行，或删除此目录。" >&2
  exit 1
else
  git clone "$REPO_URL" "$install_dir"
fi

cd "$install_dir"
chmod +x scripts/*.sh
# 通过 curl ... | bash 运行时标准输入是脚本本身，交互问题改从终端读取。
if [ ! -t 0 ] && [ -r /dev/tty ]; then
  ./scripts/setup.sh < /dev/tty
else
  ./scripts/setup.sh
fi
