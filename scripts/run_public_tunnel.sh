#!/usr/bin/env bash
# Run the Cafe OS API and publish it on a public https:// URL.
#
#   API_KEY=$(openssl rand -hex 16) ./scripts/run_public_tunnel.sh
#
# Starts uvicorn and a cloudflared quick tunnel, restarts either if it dies,
# and writes the current public URL to $LOG_DIR/public-url.txt (default
# /tmp/cafe-os/public-url.txt).
#
# Requires: the project installed (`pip install -e ".[dev]"`) and the
# cloudflared binary
# (<https://developers.cloudflare.com/cloudflare-one/connections/download-cloudflared>).

set -uo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-./.venv/bin/python}"
PORT="${PORT:-8000}"
DB_PATH="${DB_PATH:-$PWD/cafe_os.db}"
LOG_DIR="${LOG_DIR:-/tmp/cafe-os}"
API_KEY="${API_KEY:-}"

command -v cloudflared >/dev/null || { echo "cloudflared not found in PATH" >&2; exit 1; }
[ -x "$PYTHON" ] || { echo "python not found at $PYTHON (set PYTHON=...)" >&2; exit 1; }

mkdir -p "$LOG_DIR"
export DB_PATH API_KEY

if [ ! -f "$DB_PATH" ]; then
  echo "[init] seeding $DB_PATH"
  "$PYTHON" scripts/seed_db.py --db "$DB_PATH"
fi

API_PID=""
TUNNEL_PID=""

cleanup() {
  [ -n "$TUNNEL_PID" ] && kill "$TUNNEL_PID" 2>/dev/null
  [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null
  exit 0
}
trap cleanup EXIT INT TERM

echo "[init] starting uvicorn on 0.0.0.0:$PORT"
(
  while true; do
    "$PYTHON" -m uvicorn cafe_os.main:app --host 0.0.0.0 --port "$PORT" \
      --proxy-headers --forwarded-allow-ips="*" >> "$LOG_DIR/api.log" 2>&1
    echo "[supervisor] uvicorn exited rc=$? — restarting in 2s" >> "$LOG_DIR/api.log"
    sleep 2
  done
) &
API_PID=$!

until curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; do sleep 1; done
echo "[init] api healthy"

echo "[init] starting cloudflared quick tunnel"
(
  while true; do
    rm -f "$LOG_DIR/cloudflared.log"
    cloudflared tunnel --url "http://127.0.0.1:$PORT" --protocol http2 --no-autoupdate \
      --logfile "$LOG_DIR/cloudflared.log" &
    CF_PID=$!
    for _ in $(seq 1 90); do
      u="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_DIR/cloudflared.log" 2>/dev/null | head -1)"
      if [ -n "$u" ]; then
        echo "$u" > "$LOG_DIR/public-url.txt"
        echo "[tunnel] live at $u"
        break
      fi
      sleep 1
    done
    wait $CF_PID
    echo "[supervisor] cloudflared exited rc=$? — restarting in 5s"
    sleep 5
  done
) &
TUNNEL_PID=$!

URL=""
for _ in $(seq 1 90); do
  URL="$(cat "$LOG_DIR/public-url.txt" 2>/dev/null)"
  [ -n "$URL" ] && break
  sleep 1
done

if [ -z "$URL" ]; then
  echo "[error] tunnel did not come up; see $LOG_DIR/cloudflared.log" >&2
  exit 1
fi

if [ -n "$API_KEY" ]; then
  AUTH="X-API-Key: $API_KEY"
  echo "[init] API key authentication ENABLED"
else
  AUTH=""
  echo "[init] API key authentication DISABLED (set API_KEY to enable)"
fi

cat <<EOF

Cafe OS is live.

  API     $URL/api/v1/menu
  Docs    $URL/docs
  Repl    $URL/redoc
  Health  $URL/healthz
  KDS WS  ${URL/https:/wss:}/ws/kds

  curl -H "$AUTH" "$URL/api/v1/menu"

  Current URL: $(cat "$LOG_DIR/public-url.txt")

EOF

wait "$API_PID"
