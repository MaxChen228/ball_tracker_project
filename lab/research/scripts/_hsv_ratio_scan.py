"""Scan ALL GT masks across propagate-done sessions, compute HSV-blue
intersection ratio per frame, dump distribution + flag low-ratio frames."""
from __future__ import annotations
import json
import sys
from pathlib import Path
import numpy as np
import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WS, OUT, load_manifest, SEG_BY_SLUG, read_mask

_BLUE_HSV_LO = np.array([105, 140, 40], dtype=np.uint8)
_BLUE_HSV_HI = np.array([112, 255, 255], dtype=np.uint8)


def scan_session(slug: str, in_f: int) -> list[dict]:
    seg_id = SEG_BY_SLUG[slug]
    masks_dir = WS / "items" / slug / "masks" / seg_id
    frames_dir = WS / "items" / slug / "frames"
    rows = []
    for mp in sorted(masks_dir.glob("*.png")):
        src = int(mp.stem)
        local = src - in_f
        fp = frames_dir / f"{local:05d}.jpg"
        if not fp.exists():
            continue
        bgr = cv2.imread(str(fp))
        m = read_mask(mp)
        if bgr is None or m is None:
            continue
        area = int((m > 0).sum())
        if area < 5:
            continue
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        hsv_mask = cv2.inRange(hsv, _BLUE_HSV_LO, _BLUE_HSV_HI)
        inter = cv2.bitwise_and(m, hsv_mask)
        hsv_area = int((inter > 0).sum())
        rows.append({"src": src, "area": area, "hsv_area": hsv_area,
                     "ratio": round(hsv_area / area, 3)})
    return rows


def main():
    manifest = load_manifest()
    sessions = [it for it in manifest["items"] if it.get("propagate_status") == "done"]
    out = {}
    print(f"{'slug':<28} {'N':>4} {'r<0.1':>6} {'r<0.3':>6} {'r<0.6':>6} {'r>=0.6':>7}  med")
    print("-" * 72)
    grand = {"r<0.1": 0, "r<0.3": 0, "r<0.6": 0, "r>=0.6": 0, "N": 0}
    for it in sessions:
        slug = it["slug"]
        rows = scan_session(slug, it["in_frame"])
        ratios = np.array([r["ratio"] for r in rows])
        n = len(rows)
        b1 = int((ratios < 0.1).sum())
        b3 = int((ratios < 0.3).sum())
        b6 = int((ratios < 0.6).sum())
        bg = int((ratios >= 0.6).sum())
        med = float(np.median(ratios)) if n else 0.0
        print(f"{slug:<28} {n:>4} {b1:>6} {b3:>6} {b6:>6} {bg:>7}  {med:.2f}")
        for k, v in (("r<0.1", b1), ("r<0.3", b3), ("r<0.6", b6), ("r>=0.6", bg)):
            grand[k] += v
        grand["N"] += n
        out[slug] = rows
    print("-" * 72)
    print(f"{'TOTAL':<28} {grand['N']:>4} {grand['r<0.1']:>6} {grand['r<0.3']:>6} "
          f"{grand['r<0.6']:>6} {grand['r>=0.6']:>7}")
    out_path = OUT / "_mask_audit" / "hsv_ratio_per_frame.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
