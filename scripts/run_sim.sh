#!/usr/bin/env bash
# Simulator -> collector -> server. Usage: run_sim.sh [scenario] [speed] [simulator options]
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
SCENARIO="${1:-normal}"
SPEED="${2:-60}"
shift $(( $# < 2 ? $# : 2 ))

# The server must be free before its database is replaced.
python - <<'PY' || { echo "Another server is already running; stop it first." >&2; exit 1; }
import socket, sys
from server.config import load_config
c = load_config().server
host = "127.0.0.1" if c.host == "0.0.0.0" else c.host
with socket.socket() as s:
    s.settimeout(1)
    sys.exit(s.connect_ex((host, c.port)) == 0)
PY

# Simulated data never touches the real database, and each run starts afresh:
# simulated time overlaps between runs.
if [ -z "${PIP_DB_PATH:-}" ]; then
  export PIP_DB_PATH=data/simulator.db PIP_SPOOL_PATH=data/simulator-spool.jsonl
  rm -f data/simulator.db data/simulator.db-wal data/simulator.db-shm
  rm -f data/simulator-spool.jsonl data/simulator-spool.jsonl.pos
fi

python -m server.app &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
sleep 2
if ! kill -0 $SERVER_PID 2>/dev/null; then
  echo "The server did not start. Is another one already running?" >&2
  exit 1
fi
python -u -m collector.simulator --scenario "$SCENARIO" --speed "$SPEED" "$@" \
  | python -u -m collector.collector --source simulator
