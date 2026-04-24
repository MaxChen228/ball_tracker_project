"""Render ArUco markers (DICT_4X4_50, IDs 0-8) as a single printable PNG.

Layout matches `PLATE_MARKER_WORLD` in `calibration_solver.py` — a 3x3
grid on the plate plane (Z=0):
  - FL (front left)       ID 0  at (-21.6 cm,  0  cm)
  - FR (front right)      ID 1  at (+21.6 cm,  0  cm)
  - RS (right shoulder)   ID 2  at (+21.6 cm, 21.6 cm)
  - LS (left shoulder)    ID 3  at (-21.6 cm, 21.6 cm)
  - BT (back tip)         ID 4  at (  0  cm, 43.2 cm)
  - MF (mid-front edge)   ID 5  at (  0  cm,  0  cm)
  - C  (centre)           ID 6  at (  0  cm, 21.6 cm)
  - BL (back-left)        ID 7  at (-21.6 cm, 43.2 cm)
  - BR (back-right)       ID 8  at (+21.6 cm, 43.2 cm)

Print, cut out, tape each marker so its **center** sits exactly on the
labelled plate landmark. 9 points give RANSAC plenty of slack against
occlusion + misreads.

Usage:
    uv run python print_aruco_markers.py \
        --marker-size-m 0.05 --pixels-per-m 4000 --out markers.png
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np


LABELS = {0: "FL", 1: "FR", 2: "RS", 3: "LS", 4: "BT", 5: "MF",
          6: "C",  7: "BL", 8: "BR"}


def render_marker_sheet(marker_size_m: float, pixels_per_m: int, out: Path) -> None:
    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)

    # Pixel canvas per marker.
    size_px = max(120, int(marker_size_m * pixels_per_m))
    margin_px = max(40, size_px // 4)
    label_px = 60  # extra space below each marker for the "FL"/"FR"/... text

    # 3x3 grid for the 9 plate-landmark markers on A4 portrait.
    rows, cols = 3, 3
    n_markers = len(LABELS)
    w = cols * size_px + (cols + 1) * margin_px
    h = rows * (size_px + label_px) + (rows + 1) * margin_px
    sheet = np.full((h, w), 255, dtype=np.uint8)

    for i in range(n_markers):
        r, c = i // cols, i % cols
        x = margin_px + c * (size_px + margin_px)
        y = margin_px + r * (size_px + label_px + margin_px)

        img = cv2.aruco.generateImageMarker(aruco_dict, i, size_px)
        sheet[y : y + size_px, x : x + size_px] = img

        label = f"ID {i}  ({LABELS[i]})"
        cv2.putText(
            sheet, label,
            (x, y + size_px + 40),
            cv2.FONT_HERSHEY_SIMPLEX, 1.0, 0, 2, cv2.LINE_AA,
        )

    if not cv2.imwrite(str(out), sheet):
        raise SystemExit(f"failed to write {out}")
    print(f"wrote {out}  (marker size = {marker_size_m*100:.1f} cm @ {pixels_per_m} px/m)")
    print("Print at 1:1 scale. Measure one marker with a ruler to verify before use.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--marker-size-m", type=float, default=0.05,
                    help="edge length of each ArUco square in meters (default 5 cm)")
    ap.add_argument("--pixels-per-m", type=int, default=4000,
                    help="print DPI proxy; 4000 px/m ≈ 102 dpi at 1:1")
    ap.add_argument("--out", type=Path, required=True, help="output PNG path")
    args = ap.parse_args(argv)
    render_marker_sheet(args.marker_size_m, args.pixels_per_m, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
