#!/usr/bin/env bash
# Show the links to the running dashboard. "share.sh public" also opens a public
# link with Tailscale Funnel until Ctrl-C.
set -euo pipefail
cd "$(dirname "$0")/.."
read -r HOST PORT < <(.venv/bin/python -c 'from server.config import load_config as c; s = c().server; print(s.host, s.port)')
if ! curl -sf -o /dev/null "http://127.0.0.1:$PORT/"; then
  echo "The station is not running. Start scripts/run_mac.sh first." >&2
  exit 1
fi
echo "This computer:  http://127.0.0.1:$PORT   (the only place to change settings)"
if [ "$HOST" = "0.0.0.0" ]; then
  IP="$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || true)"
  if [ -n "$IP" ]; then echo "Same Wi-Fi:     http://$IP:$PORT"; else echo "Same Wi-Fi:     no network found"; fi
else
  echo "Same Wi-Fi:     off (set host = \"0.0.0.0\" under [server] in config.toml)"
fi
[ "${1:-}" = "public" ] || exit 0
TS="$(command -v tailscale || true)"
[ -n "$TS" ] || TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
[ -x "$TS" ] || { echo "Tailscale is not installed: https://tailscale.com/download" >&2; exit 1; }
echo "Anyone with the link (Ctrl-C stops sharing):"
exec "$TS" funnel "$PORT"
