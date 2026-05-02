#!/usr/bin/env python3
"""Backfill scrub-proxy JPEG strips + width/height for existing items.

Lab front-end was rewritten to scrub via `<img src="/proxy/...">` swap
(industry-standard pre-extracted thumbnail strip, like Premiere/DaVinci).
Existing items in standalone_workspace/items/ ingested before this change
have no proxy_frames/<slug>/ on disk and no width/height in manifest.json.

Idempotent: re-runs only re-extract clips whose source mtime differs from
the cached `proxy_frames/<slug>/done.flag`. width/height also only
backfilled if missing in manifest.

Usage:
    lab/.venvs/sam2_probe/bin/python lab/migrate_extract_proxies.py [--dry-run]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Reuse labeller helpers; this script imports the module rather than
# duplicating the ffmpeg invocation so any tweak (PROXY_WIDTH, -q:v) lands
# in one place.
LAB = Path(__file__).resolve().parent
sys.path.insert(0, str(LAB.parent))

from lab.labeller import (  # noqa: E402
    MANIFEST_PATH,
    PROXY_DIR,
    SOURCES_DIR,
    _extract_proxy_frames,
    _video_meta,
)


def main() -> int:
    dry = "--dry-run" in sys.argv
    if not MANIFEST_PATH.exists():
        print(f"no manifest at {MANIFEST_PATH}", file=sys.stderr)
        return 1
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    items = payload.get("items", [])
    extracted = skipped = backfilled_dims = errors = 0
    for it in items:
        slug = it["slug"]
        src = SOURCES_DIR / it["source_video"]
        if not src.is_file():
            print(f"WARN missing source for {slug}: {src.name}")
            errors += 1
            continue
        # 1. Backfill width/height in manifest from ffprobe.
        if "width" not in it or "height" not in it:
            try:
                meta = _video_meta(src)
                if dry:
                    print(f"would set dims {meta['width']}x{meta['height']}: {slug}")
                else:
                    it["width"] = meta["width"]
                    it["height"] = meta["height"]
                backfilled_dims += 1
            except Exception as e:
                print(f"FAIL ffprobe {slug}: {e}")
                errors += 1
                continue
        # 2. Extract proxy strip (idempotent via done.flag).
        flag = PROXY_DIR / slug / "done.flag"
        if flag.exists():
            try:
                cached = float(flag.read_text(encoding="utf-8").strip())
                if abs(cached - src.stat().st_mtime) < 1e-3:
                    skipped += 1
                    continue
            except (ValueError, OSError):
                pass
        if dry:
            print(f"would extract proxies: {slug}")
            extracted += 1
            continue
        try:
            n = _extract_proxy_frames(src, slug)
            print(f"ok  {slug}: {n} frames")
            extracted += 1
        except Exception as e:
            print(f"FAIL extract {slug}: {e}")
            errors += 1

    if not dry and backfilled_dims:
        MANIFEST_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(
        f"\ndone: extracted={extracted} skipped={skipped} "
        f"dims_backfilled={backfilled_dims} errors={errors} dry={dry}"
    )
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
