#!/usr/bin/env bash
# Bootstrap and publish the Cafe OS API on a public https:// URL from a bare
# container (no Python, no venv, no cloudflared required beforehand).
#
#   ./scripts/bootstrap_deploy.sh            # generates a fresh API key
#   API_KEY=... ./scripts/bootstrap_deploy.sh
#
# Steps: install system Python (apt) -> create venv -> install project deps ->
# fetch cloudflared -> seed the database -> start supervised API + tunnel.
# The public URL is written to $LOG_DIR/public-url.txt and printed on stdout.

set -euo pipefail

cd "$(dirname "$0")/.."

LOG_DIR="${LOG_DIR:-$PWD/.deploy}"
PYTHON="${PYTHON:-$PWD/.venv/bin/python}"
PORT="${PORT:-8000}"
DB_PATH="${DB_PATH:-$PWD/cafe_os.db}"
API_KEY="${API_KEY:-}"

log() { echo "[bootstrap] $*"; }

if ! command -v python3 >/dev/null 2>&1; then
  log "installing python3"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip
fi

if [ ! -x "$PYTHON" ]; then
  log "creating venv"
  VENV_DIR="$(dirname "$(dirname "$PYTHON")")"
  rm -rf "$VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

log "installing project dependencies"
"$PYTHON" -m pip install -q --upgrade pip wheel
"$PYTHON" -m pip install -q -e ".[dev]"

if ! command -v cloudflared >/dev/null 2>&1; then
  log "fetching cloudflared"
  arch="$(uname -m)"
  case "$arch" in
    x86_64) cf_arch=amd64 ;;
    aarch64 | arm64) cf_arch=arm64 ;;
    *) echo "unsupported arch: $arch" >&2; exit 1 ;;
  esac
  curl -fsSL -o /tmp/cloudflared \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${cf_arch}"
  install -m 0755 /tmp/cloudflared /usr/local/bin/cloudflared
fi

if [ -z "$API_KEY" ]; then
  API_KEY="$(openssl rand -hex 16 2>/dev/null || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi
log "api key: $API_KEY"

mkdir -p "$LOG_DIR"
printf '%s\n' "$API_KEY" > "$LOG_DIR/api-key.txt"

export API_KEY DB_PATH LOG_DIR PORT PYTHON
exec ./scripts/run_public_tunnel.sh
