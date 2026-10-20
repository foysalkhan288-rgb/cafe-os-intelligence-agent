#!/usr/bin/env bash
# Print the current public URL of the Cafe OS deployment started by
# scripts/run_public_tunnel.sh (and a ready-to-run curl command).
#
#   ./scripts/public_url.sh
#   API_KEY=... ./scripts/public_url.sh

set -uo pipefail

LOG_DIR="${LOG_DIR:-/tmp/cafe-os}"
URL_FILE="$LOG_DIR/public-url.txt"

if [ ! -s "$URL_FILE" ]; then
  echo "no public URL recorded at $URL_FILE — is the tunnel running?" >&2
  exit 1
fi

URL="$(cat "$URL_FILE")"
echo "$URL"
if [ -n "${API_KEY:-}" ]; then
  echo "curl -H \"X-API-Key: $API_KEY\" \"$URL/api/v1/menu\""
else
  echo "curl \"$URL/api/v1/menu\""
fi
