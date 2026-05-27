#!/usr/bin/env bash
# Start the Godot trajectory viewer (sim/) on this machine.
# Usage: ./start_sim.sh             # foreground: open Godot with sim/project.godot
#        ./start_sim.sh --headless  # rare: run Godot from CLI without window
#
# Symmetric to start.sh (which runs the server). Run both for the
# end-to-end stack:
#
#   terminal 1: ./start.sh
#   terminal 2: ./start_sim.sh
#
# Godot connects to the server's /sim/events WS on _Ready and auto-loads
# any live session that finishes with non-empty segments. Manual session
# lookup via the in-app UI still works regardless of WS state.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIM_PROJECT="$ROOT/sim/project.godot"
GODOT_APP="/Applications/Godot_mono.app"
GODOT_BIN="$GODOT_APP/Contents/MacOS/Godot"

# Pre-flight: hard-fail if the toolchain isn't ready. Each branch points
# at the install command so a fresh machine can self-diagnose without
# grepping README files.
if [[ ! -f "$SIM_PROJECT" ]]; then
  echo "[start_sim.sh] missing $SIM_PROJECT" >&2
  exit 1
fi
if [[ ! -d "$GODOT_APP" ]]; then
  echo "[start_sim.sh] Godot 4.6 .NET not at $GODOT_APP" >&2
  echo "              Install: see sim/README.md (or curl the official zip)" >&2
  exit 1
fi
if ! command -v dotnet >/dev/null 2>&1; then
  echo "[start_sim.sh] dotnet not on PATH — Godot will fail to build C#" >&2
  echo "              Install: brew install dotnet@8 (and source ~/.zshrc)" >&2
  exit 1
fi

# Kill stale Godot instances bound to this project so re-running doesn't
# pile up windows. We match the project path to avoid touching unrelated
# Godot work the operator might have open.
STALE_PIDS="$(pgrep -fl "Godot.*sim/project.godot" 2>/dev/null | awk '{print $1}' || true)"
if [[ -n "$STALE_PIDS" ]]; then
  echo "[start_sim.sh] killing stale Godot pids: $STALE_PIDS"
  echo "$STALE_PIDS" | xargs kill -9 2>/dev/null || true
  sleep 0.3
fi

# Surface the server URL the viewer will try; helps the operator notice
# when they forgot to start ./start.sh first. (Godot itself will retry
# WS connect on a 3 s back-off, so a missing server is not fatal — it
# just sits idle until you start one.)
SERVER_URL="http://127.0.0.1:${PORT:-8765}"
if curl -fsS --max-time 1 "$SERVER_URL/status" >/dev/null 2>&1; then
  SERVER_STATE="✓ reachable"
else
  SERVER_STATE="✗ NOT reachable — start ./start.sh in another terminal"
fi

cat <<EOF

  ball_tracker_sim — Godot trajectory viewer
  ──────────────────────────────────────────
  Project        $SIM_PROJECT
  Godot          $(/Applications/Godot_mono.app/Contents/MacOS/Godot --version 2>/dev/null | head -1)
  dotnet         $(dotnet --version 2>/dev/null)
  Server         $SERVER_URL   $SERVER_STATE

  Push channel   ws://127.0.0.1:${PORT:-8765}/sim/events
                 auto-loads any live session that finishes with segments
  Manual load    type session id (s_xxx) in the viewer UI and click Load

EOF

if [[ "${1:-}" == "--headless" ]]; then
  # Headless mode: run Godot from CLI so its build / import log lands in
  # this terminal. Useful for CI-style "does it boot" checks. Closes
  # immediately after editor import is done.
  cd "$ROOT/sim"
  exec "$GODOT_BIN" --headless --editor --quit
fi

# Default: open the app bundle so Godot runs in its own window and we
# return immediately. `open -W` would block; we want this script to be
# a launcher, not a babysitter.
exec open -a "$GODOT_APP" "$SIM_PROJECT"
