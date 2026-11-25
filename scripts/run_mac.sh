#!/usr/bin/env bash
# Real receiver on macOS. No root needed: no driver claims the receiver.
set -euo pipefail
cd "$(dirname "$0")/.."
RECV_DIR="${RECV_DIR:-$PWD/PIPtagCode/ReceiverCode}"
STDBUF="$(command -v gstdbuf || command -v stdbuf)" || { echo "stdbuf not found (brew install coreutils)" >&2; exit 1; }
export RECV_DIR STDBUF
source .venv/bin/activate
python -m server.app &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
sleep 2
if ! kill -0 $SERVER_PID 2>/dev/null; then
  echo "The server did not start. Is another one already running?" >&2
  exit 1
fi
# caffeinate keeps the Mac from sleeping, which would stop data collection.
caffeinate -i bash -c '"$STDBUF" -o0 "$RECV_DIR/pip_sense.v2" l l \
  | python -u -m collector.collector --source hardware --record'
