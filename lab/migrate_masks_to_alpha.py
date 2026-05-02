#!/usr/bin/env python3
"""Re-encode existing mask PNGs from L mode to LA mode (alpha = mask).

Front-end switched from per-pixel JS tinting to GPU `destination-in`
composite. That requires masks to carry the binary in the alpha channel.

Run BEFORE deploying the new front-end. Stop the labeller server first;
otherwise mid-propagation writes can race with the rewrite.

Usage:
    lab/.venvs/sam2_probe/bin/python lab/migrate_masks_to_alpha.py [--dry-run]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

LAB = Path(__file__).resolve().parent
# Mirrors labeller.py:25-27 (WORKSPACE = LAB_DIR / "standalone_workspace").
ITEMS = LAB / "standalone_workspace" / "items"


def migrate(p: Path) -> str:
    with Image.open(p) as im:
        im.load()
        mode = im.mode
        if mode == "LA":
            return "skip"
        if mode != "L":
            return f"unexpected mode={mode}"
        arr = np.asarray(im, dtype=np.uint8)
    la = np.stack([np.zeros_like(arr), arr], axis=-1)
    Image.fromarray(la, mode="LA").save(p, format="PNG", optimize=False)
    return "ok"


def main() -> int:
    if not ITEMS.is_dir():
        print(f"no items dir at {ITEMS}", file=sys.stderr)
        return 1
    dry = "--dry-run" in sys.argv
    counts = {"ok": 0, "skip": 0, "err": 0}
    for png in sorted(ITEMS.glob("*/masks/**/*.png")):
        try:
            res = migrate(png) if not dry else _peek_mode(png)
            if res == "ok":
                counts["ok"] += 1
                print(f"ok    {png.relative_to(LAB)}")
            elif res == "skip":
                counts["skip"] += 1
            elif res == "would-convert":
                counts["ok"] += 1
                print(f"would {png.relative_to(LAB)}")
            else:
                counts["err"] += 1
                print(f"WARN  {png.relative_to(LAB)}: {res}")
        except Exception as e:
            counts["err"] += 1
            print(f"FAIL  {png}: {e}")
    print(f"\ndone: ok={counts['ok']} skip={counts['skip']} err={counts['err']} dry={dry}")
    return 0 if counts["err"] == 0 else 2


def _peek_mode(p: Path) -> str:
    with Image.open(p) as im:
        if im.mode == "LA":
            return "skip"
        if im.mode == "L":
            return "would-convert"
        return f"unexpected mode={im.mode}"


if __name__ == "__main__":
    sys.exit(main())
