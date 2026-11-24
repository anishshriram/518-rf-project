#!/usr/bin/env bash
# Real receiver on macOS. Only pip_sense.v2 runs as root.
set -euo pipefail
cd "$(dirname "$0")/.."
RECV_DIR="${RECV_DIR:-/Users/anishshriram/Downloads/PIPtagCode/ReceiverCode}"
STDBUF="$(command -v gstdbuf || command -v stdbuf)" || { echo "stdbuf not found (brew install coreutils)" >&2; exit 1; }
export RECV_DIR STDBUF
source .venv/bin/activate
sudo -v
python -m server.app &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
sleep 1
# caffeinate keeps the Mac from sleeping, which would stop data collection.
caffeinate -i bash -c 'sudo "$STDBUF" -o0 "$RECV_DIR/pip_sense.v2" l l \
  | python -u -m collector.collector --source hardware --record'
