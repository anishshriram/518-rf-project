#!/usr/bin/env bash
# Save raw receiver output to logs/YYYY-MM-DD.log without running the server.
set -euo pipefail
cd "$(dirname "$0")/.."
RECV_DIR="${RECV_DIR:-$PWD/PIPtagCode/ReceiverCode}"
STDBUF="$(command -v gstdbuf || command -v stdbuf)" || { echo "stdbuf not found (macOS: brew install coreutils)" >&2; exit 1; }
mkdir -p logs
"$STDBUF" -o0 "$RECV_DIR/pip_sense.v2" l l | tee -a "logs/$(date +%F).log"
