#!/usr/bin/env bash
# Start or restart the ball_tracker FastAPI server.
# Usage: ./start.sh [port]   (default port 8765)

set -euo pipefail

PORT="${1:-8765}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$ROOT/server"

# Kill anything already bound to the port (previous uvicorn instance).
if lsof -ti tcp:"$PORT" >/dev/null 2>&1; then
  echo "[start.sh] killing existing process on port $PORT"
  lsof -ti tcp:"$PORT" | xargs kill -9 || true
  sleep 0.5
fi

# Print LAN IP so you can paste it into the iPhone Settings screen.
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<unknown>')"
echo "[start.sh] LAN IP: $LAN_IP   port: $PORT"
echo "[start.sh] iPhone Settings -> Server IP: $LAN_IP   Port: $PORT"

cd "$SERVER_DIR"
exec uv run uvicorn main:app --host 0.0.0.0 --port "$PORT"
