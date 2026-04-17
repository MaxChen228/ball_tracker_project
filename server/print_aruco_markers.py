"""Render ArUco markers (DICT_4X4_50, IDs 0-3) as a single printable PNG.

Layout matches `CalibrationViewController.markerWorldPoints` in the iOS app:
four markers centered on the four home-plate corners FL / FR / RS / LS at
(±21.6 cm, 0 or 21.6 cm) — print on A4, cut out, tape each marker so its
**center** sits exactly on the labelled plate vertex.

Usage:
    uv run python print_aruco_markers.py \
        --marker-size-m 0.05 --pixels-per-m 4000 --out markers.png
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np


LABELS = {0: "FL", 1: "FR", 2: "RS", 3: "LS"}


def render_marker_sheet(marker_size_m: float, pixels_per_m: int, out: Path) -> None:
    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)

    # Pixel canvas per marker.
    size_px = max(120, int(marker_size_m * pixels_per_m))
    margin_px = max(40, size_px // 4)
    label_px = 60  # extra space below each marker for the "FL"/"FR"/... text

    # 2x2 grid to fit A4 portrait.
    rows, cols = 2, 2
    w = cols * size_px + (cols + 1) * margin_px
    h = rows * (size_px + label_px) + (rows + 1) * margin_px
    sheet = np.full((h, w), 255, dtype=np.uint8)

    for i in range(4):
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
