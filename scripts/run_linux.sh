#!/usr/bin/env bash
# Real receiver on Linux. Only pip_sense.v2 runs as root.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${RECV_DIR:?Set RECV_DIR to the directory containing pip_sense.v2}"
source .venv/bin/activate
sudo -v
python -m server.app &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
sleep 1
sudo stdbuf -o0 "$RECV_DIR/pip_sense.v2" l l \
  | python -u -m collector.collector --source hardware --record
