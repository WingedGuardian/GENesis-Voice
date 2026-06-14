#!/usr/bin/env bash
# GENesis-Voice edge installer (starting point — review before trusting it).
#
# Sets up one or both bridges on a Voice Edge box (a dedicated VM or container,
# NOT Home Assistant OS and NOT the Genesis box). Creates a venv per bridge,
# installs deps, and stages the systemd user units. It does NOT flash firmware
# and does NOT write your secrets — see docs/SETUP.md for those steps.
#
# Usage:  deploy/install.sh [s2s|ambient|both]   (default: both)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-both}"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

have() { command -v "$1" >/dev/null 2>&1; }

require_python() {
  have python3 || { echo "python3 not found — install Python 3.11+ first." >&2; exit 1; }
}

install_s2s() {
  echo ">> s2s bridge"
  local venv="$HOME/s2s-venv"
  python3 -m venv "$venv"
  "$venv/bin/pip" install --upgrade pip
  # Matches the add-on's runtime set (see bridges/s2s_bridge/Dockerfile).
  "$venv/bin/pip" install \
    "pipecat-ai[openai,websocket]==1.3.0" openai "websockets>=13.0" \
    "fastapi>=0.115.0" "uvicorn[standard]>=0.23.0" python-dotenv httpx \
    aiohttp aiofiles pydantic loguru numpy Pillow protobuf nltk Markdown \
    soxr pyloudnorm docstring_parser onnxruntime
  cp "$REPO_ROOT/deploy/systemd/s2s-bridge.service" "$SYSTEMD_USER_DIR/"
  echo "   venv: $venv"
  echo "   next: cp bridges/s2s_bridge/edge/.env.example bridges/s2s_bridge/edge/.env  (then fill it in)"
}

install_ambient() {
  echo ">> ambient bridge"
  local venv="$HOME/ambient-venv"
  python3 -m venv "$venv"
  "$venv/bin/pip" install --upgrade pip
  "$venv/bin/pip" install sherpa-onnx onnxruntime soxr soundfile websockets numpy
  cp "$REPO_ROOT/deploy/systemd/ambient-bridge.service" "$SYSTEMD_USER_DIR/"
  echo "   venv: $venv"
  echo "   next: download the STT models into ~/models (see bridges/ambient_bridge/README.md)"
}

require_python
mkdir -p "$SYSTEMD_USER_DIR"
echo "Installing into a link-friendly layout: this repo at \$HOME/genesis-voice is assumed by the units."

case "$TARGET" in
  s2s)     install_s2s ;;
  ambient) install_ambient ;;
  both)    install_s2s; install_ambient ;;
  *) echo "usage: $0 [s2s|ambient|both]" >&2; exit 2 ;;
esac

cat <<EOF

Done (deps + units staged). To start a bridge:
  systemctl --user daemon-reload
  systemctl --user enable --now s2s-bridge      # or ambient-bridge

The systemd units assume this checkout lives at \$HOME/genesis-voice and each
bridge has its venv at \$HOME/s2s-venv / \$HOME/ambient-venv. Adjust the unit
files if your paths differ. See docs/SETUP.md.
EOF
