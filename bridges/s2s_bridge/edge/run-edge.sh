#!/usr/bin/env bash
# Edge entrypoint for the s2s bridge — the VM / container path (no Home Assistant
# add-on, no bashio). Reads configuration from the environment; loads a sibling
# .env if present. The HA add-on path (root/run.sh + config.yaml) still works for
# anyone running this inside Home Assistant; this script is for everyone else.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$HERE/.." && pwd)"

# Load .env (KEY=VALUE lines) if present.
if [ -f "$HERE/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$HERE/.env"
  set +a
fi

: "${OPENAI_API_KEY:?OPENAI_API_KEY is required (see edge/.env.example)}"

export PYTHONUNBUFFERED=1
cd "$APP_DIR"
exec python3 -m app.main
