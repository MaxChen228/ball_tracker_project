"""Materialize the converged GT dataset.

Reframed criterion (correct centroid is the only thing R cares about):
  - hsv_area >= 5  : write HSV-cleaned mask (centroid sits on actual ball pixels)
  - hsv_area <  5  : drop the frame (mask drifted off ball, no recoverable centroid)

session_s_373bbf6e_b: skip entirely (whole-session GT drift to non-ball).

Output: lab/standalone_workspace/items/<slug>/masks_hsv/<seg>/*.png
"""
from __future__ import annotations
import json
import shutil
import sys
from pathlib import Path
import numpy as np
import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WS, OUT, load_manifest, SEG_BY_SLUG, read_mask

AUDIT = OUT / "_mask_audit"
_BLUE_HSV_LO = np.array([105, 140, 40], dtype=np.uint8)
_BLUE_HSV_HI = np.array([112, 255, 255], dtype=np.uint8)


def hsv_clean(bgr: np.ndarray, mask: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    hsv_mask = cv2.inRange(hsv, _BLUE_HSV_LO, _BLUE_HSV_HI)
    return cv2.bitwise_and(mask, hsv_mask)


def main():
    load_manifest()
    labels = json.loads((AUDIT / "auto_labels.json").read_text())
    in_frames = {it["slug"]: it.get("in_frame")
                 for it in load_manifest()["items"]
                 if it.get("propagate_status") == "done"}

    summary = {}
    for slug, perframe in labels.items():
        if "_status" in perframe:
            print(f"SKIP {slug} ({perframe['_reason']})")
            continue
        seg_id = SEG_BY_SLUG[slug]
        in_f = in_frames[slug]
        masks_dir = WS / "items" / slug / "masks" / seg_id
        frames_dir = WS / "items" / slug / "frames"
        out_dir = WS / "items" / slug / "masks_hsv" / seg_id
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        c = {"dropped": 0, "cleaned": 0, "kept": 0}
        for src_str, info in perframe.items():
            src = int(src_str)
            label = info["label"]
            if label == "bad":
                c["dropped"] += 1
                continue
            mp = masks_dir / f"{src:05d}.png"
            mask = read_mask(mp)
            if mask is None:
                c["dropped"] += 1
                continue
            if label == "ok":
                shutil.copy(mp, out_dir / f"{src:05d}.png")
                c["kept"] += 1
            else:  # borderline
                local = src - in_f
                fp = frames_dir / f"{local:05d}.jpg"
                bgr = cv2.imread(str(fp))
                if bgr is None:
                    c["dropped"] += 1
                    continue
                cleaned = hsv_clean(bgr, mask)
                if (cleaned > 0).sum() < 5:
                    c["dropped"] += 1
                    continue
                cv2.imwrite(str(out_dir / f"{src:05d}.png"), cleaned)
                c["cleaned"] += 1
        summary[slug] = c
        print(f"{slug:<28} drop={c['dropped']:>4}  clean={c['cleaned']:>4}  kept={c['kept']:>4}")

    grand = {"dropped": 0, "cleaned": 0, "kept": 0}
    for c in summary.values():
        for k in grand:
            grand[k] += c[k]
    print("-" * 60)
    print(f"{'TOTAL (15 blue sessions)':<28} drop={grand['dropped']:>4}  "
          f"clean={grand['cleaned']:>4}  kept={grand['kept']:>4}")
    total = sum(grand.values())
    print(f"  drop_pct={100 * grand['dropped'] / total:.1f}%  "
          f"clean_pct={100 * grand['cleaned'] / total:.1f}%  "
          f"kept_pct={100 * grand['kept'] / total:.1f}%")
    print(f"  +57 dropped from session_s_373bbf6e_b (entire session)")

    (AUDIT / "materialize_summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nCleaned GT written to lab/labeller_workspace/items/<slug>/masks_hsv/<seg>/")


if __name__ == "__main__":
    main()
