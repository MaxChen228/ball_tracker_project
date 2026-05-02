#!/usr/bin/env bash
# Reload-or-start the lab labeller. Kills any existing instance bound to the
# port, then launches a fresh process from project root so `-m lab.labeller`
# can resolve.
set -euo pipefail

PORT=8876
LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$LAB_DIR/.." && pwd)"
PYTHON="$LAB_DIR/.venvs/sam2_probe/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "[start] missing venv python: $PYTHON" >&2
  exit 1
fi

# Kill any existing listener on PORT (lsof -t prints just PIDs).
existing=$(lsof -nP -ti:"$PORT" 2>/dev/null || true)
if [[ -n "$existing" ]]; then
  echo "[start] killing existing labeller (PIDs: $existing)"
  kill $existing 2>/dev/null || true
  # wait up to 3s for the port to free
  for _ in 1 2 3 4 5 6; do
    sleep 0.5
    [[ -z "$(lsof -nP -ti:$PORT 2>/dev/null || true)" ]] && break
  done
fi

# Idempotent mask migration (L → LA mode for GPU `destination-in` composite).
# Re-encoding only happens for L-mode files; LA-mode files are skipped.
# Safe to run on every restart since the server is already killed above.
echo "[start] migrating masks (L → LA, idempotent)…"
"$PYTHON" "$LAB_DIR/migrate_masks_to_alpha.py" >/dev/null

# Idempotent proxy-frame extraction (low-res JPEG strip per item for scrub UI).
# Skips clips whose proxy_frames/<slug>/done.flag matches source mtime; first
# run on existing items can take a couple minutes (one ffmpeg pass per clip).
echo "[start] extracting scrub proxies (idempotent)…"
"$PYTHON" "$LAB_DIR/migrate_extract_proxies.py"

cd "$PROJECT_ROOT"
echo "[start] launching: $PYTHON -m lab.labeller (cwd=$PROJECT_ROOT)"
exec "$PYTHON" -m lab.labeller
