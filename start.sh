#!/usr/bin/env bash
# Start or restart the ball_tracker FastAPI server.
# Usage: ./start.sh [port]   (default port 8765)

set -euo pipefail

PORT="${1:-8765}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$ROOT/server"
DATA_DIR="${BALL_TRACKER_DATA_DIR:-$SERVER_DIR/data}"

# Kill anything already bound to the port (previous uvicorn instance).
if lsof -ti tcp:"$PORT" >/dev/null 2>&1; then
  echo "[start.sh] killing existing process on port $PORT"
  lsof -ti tcp:"$PORT" | xargs kill -9 || true
  sleep 0.5
fi

LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<unknown>')"

cat <<EOF

  ball_tracker server
  ───────────────────
  LAN IP         $LAN_IP
  Port           $PORT
  Data dir       $DATA_DIR

  Events index   http://$LAN_IP:$PORT/
  Localhost      http://localhost:$PORT/
  Status         http://$LAN_IP:$PORT/status
  Chirp .wav     http://$LAN_IP:$PORT/chirp.wav

  iPhone → Settings → Server IP: $LAN_IP   Port: $PORT
  Stop: Ctrl+C

EOF

cd "$SERVER_DIR"
exec uv run uvicorn main:app --host 0.0.0.0 --port "$PORT"
