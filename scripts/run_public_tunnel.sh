#!/usr/bin/env bash
# Start the Cafe OS API and expose it on a public https:// URL.
#
#   ./scripts/run_public_tunnel.sh
#
# Requires: python3 with the project installed (pip install -e ".[dev]") and
# the cloudflared binary (https://developers.cloudflare.com/cloudflare-one/connections/download-cloudflared).
# The script prints the public URL and keeps both processes in the foreground.

set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-./.venv/bin/python}"
PORT="${PORT:-8000}"
DB_PATH="${DB_PATH:-$PWD/cafe_os.db}"
LOG_DIR="${LOG_DIR:-/tmp/cafe-os}"

command -v cloudflared >/dev/null || { echo "cloudflared not found in PATH" >&2; exit 1; }
[ -x "$PYTHON" ] || { echo "python not found at $PYTHON (set PYTHON=...)" >&2; exit 1; }

mkdir -p "$LOG_DIR"
export DB_PATH

if [ ! -f "$DB_PATH" ]; then
  echo "[init] seeding $DB_PATH"
  "$PYTHON" scripts/seed_db.py --db "$DB_PATH"
fi

echo "[init] starting uvicorn on 0.0.0.0:$PORT"
"$PYTHON" -m uvicorn cafe_os.main:app \
  --host 0.0.0.0 --port "$PORT" --proxy-headers --forwarded-allow-ips="*" &
API_PID=$!

cleanup() {
  kill "$API_PID" 2>/dev/null || true
  kill "$TUNNEL_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

until curl -fsS "http://127.0.0.1:$PORT/api/v1/menu" >/dev/null 2>&1; do
  sleep 1
done
echo "[init] api healthy"

echo "[init] starting cloudflared quick tunnel"
cloudflared tunnel --url "http://127.0.0.1:$PORT" --protocol http2 --no-autoupdate \
  --logfile "$LOG_DIR/cloudflared.log" &
TUNNEL_PID=$!

URL=""
for _ in $(seq 1 60); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_DIR/cloudflared.log" 2>/dev/null | head -1 || true)"
  [ -n "$URL" ] && break
  sleep 1
done

if [ -z "$URL" ]; then
  echo "[error] tunnel did not come up; see $LOG_DIR/cloudflared.log" >&2
  exit 1
fi

cat <<EOF

Cafe OS is live.

  API      $URL/api/v1/menu
  Docs     $URL/docs
  Repl     $URL/redoc
  KDS WS   ${URL/https:/wss:}/ws/kds

EOF

wait "$API_PID"
