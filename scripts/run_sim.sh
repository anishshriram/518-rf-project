#!/usr/bin/env bash
# Simulator -> collector -> server. Usage: run_sim.sh [scenario] [speed] [simulator options]
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
SCENARIO="${1:-normal}"
SPEED="${2:-60}"
shift $(( $# < 2 ? $# : 2 ))
python -m server.app &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
sleep 1
python -u -m collector.simulator --scenario "$SCENARIO" --speed "$SPEED" "$@" \
  | python -u -m collector.collector --source simulator
