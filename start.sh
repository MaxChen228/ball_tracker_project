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
# --no-access-log: silence per-request HTTP access lines. Steady state is
# ~30 lines/s (heartbeat 1 Hz + preview_frame 10 Hz per cam, dashboard
# polling 5 Hz per preview, /status 1 Hz) which buries every app-level
# info log (detection, calibration, drift warnings). App logs unaffected.
# Re-enable with BALL_TRACKER_ACCESS_LOG=1 ./start.sh for HTTP debug.
ACCESS_FLAG="--no-access-log"
if [ "${BALL_TRACKER_ACCESS_LOG:-0}" = "1" ]; then
  ACCESS_FLAG="--access-log"
fi
# `cv2` (opencv-contrib-python-headless) and `av` (PyAV) each bundle their
# own copy of FFmpeg's libavdevice. Both register the AVFFrameReceiver
# and AVFAudioReceiver Obj-C classes, so dyld emits two
# `objc[pid]: Class … is implemented in both …` warnings on every boot.
# Neither library actually uses libavdevice on this server's hot path
# (cv2 only does inRange / connectedComponents / aruco; PyAV reads files
# via libavformat), so the warnings are cosmetic — but they bury real
# stderr. Filter only the `objc[pid]:` lines; everything else still goes
# to stderr untouched. `--line-buffered` so the filter doesn't hold
# lines in pipe buffers waiting for more output.
exec uv run uvicorn main:app --host 0.0.0.0 --port "$PORT" $ACCESS_FLAG \
  2> >(grep --line-buffered -v '^objc\[[0-9]\+\]:' >&2)
