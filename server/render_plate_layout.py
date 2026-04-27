"""Render a top-down reference image of the plate ArUco layout, with the
real DICT_4X4_50 marker bitmaps drawn at their world positions.

Use it to spot-check tape placement after sticking IDs 0-8 on / around
home plate. Each marker is drawn as the actual bitmap that the iOS /
server detector will see; the print script (`print_aruco_markers.py`) is
the 1:1 printable companion.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

from calibration_solver import PLATE_MARKER_WORLD

LABELS = {0: "FL", 1: "FR", 2: "RS", 3: "LS", 4: "BT",
          5: "MF", 6: "C",  7: "BL", 8: "BR"}

# real plate constants (must match calibration_solver)
W = 0.432
SHOULDER_Y = 0.216
TIP_Y = 0.432

# rendering: 1200 px/m -> plate ~518 px; bbox 0.432 x 0.432 m
PX_PER_M = 1600.0
MARGIN_M = 0.16
DEFAULT_MARKER_SIZE_M = 0.05  # matches print_aruco_markers.py default

# canvas extents in world coords
X_MIN, X_MAX = -W / 2 - MARGIN_M, W / 2 + MARGIN_M
Y_MIN, Y_MAX = -MARGIN_M, TIP_Y + MARGIN_M
W_PX = int(round((X_MAX - X_MIN) * PX_PER_M))
H_PX = int(round((Y_MAX - Y_MIN) * PX_PER_M))

# colors (BGR)
BG = (250, 247, 240)         # warm off-white
PLATE_FILL = (235, 228, 215)
PLATE_EDGE = (60, 60, 60)
GRID = (210, 205, 195)
TEXT = (30, 30, 30)
AXIS = (120, 120, 120)
OFFPLATE_RING = (90, 90, 200)  # red-ish ring around BL/BR (BGR)
ONPLATE_RING = (60, 130, 60)   # green ring around 0-6


def w2p(x: float, y: float) -> tuple[int, int]:
    """World (m) -> pixel. Pitcher at bottom, catcher at top: flip Y."""
    px = int(round((x - X_MIN) * PX_PER_M))
    py = int(round((Y_MAX - y) * PX_PER_M))
    return px, py


def draw(marker_size_m: float = DEFAULT_MARKER_SIZE_M):
    img = np.full((H_PX, W_PX, 3), BG, dtype=np.uint8)
    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)

    # grid every 0.1 m
    g = 0.1
    y = np.floor(Y_MIN / g) * g
    while y <= Y_MAX + 1e-9:
        p1 = w2p(X_MIN, y); p2 = w2p(X_MAX, y)
        cv2.line(img, p1, p2, GRID, 1, cv2.LINE_AA)
        y += g
    x = np.floor(X_MIN / g) * g
    while x <= X_MAX + 1e-9:
        p1 = w2p(x, Y_MIN); p2 = w2p(x, Y_MAX)
        cv2.line(img, p1, p2, GRID, 1, cv2.LINE_AA)
        x += g

    # plate pentagon: FL -> FR -> RS -> BT -> LS -> FL
    pent_world = [
        (-W / 2, 0.0),       # FL
        ( W / 2, 0.0),       # FR
        ( W / 2, SHOULDER_Y),# RS
        ( 0.0,   TIP_Y),     # BT
        (-W / 2, SHOULDER_Y),# LS
    ]
    pent_px = np.array([w2p(*p) for p in pent_world], dtype=np.int32)
    cv2.fillPoly(img, [pent_px], PLATE_FILL)
    cv2.polylines(img, [pent_px], True, PLATE_EDGE, 3, cv2.LINE_AA)

    # markers — render real DICT_4X4_50 bitmaps at world positions
    half = marker_size_m / 2.0
    marker_px = max(60, int(round(marker_size_m * PX_PER_M)))
    font = cv2.FONT_HERSHEY_SIMPLEX
    for mid, (wx, wy) in PLATE_MARKER_WORLD.items():
        off_plate = mid in (7, 8)
        gray = cv2.aruco.generateImageMarker(aruco_dict, mid, marker_px)
        bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        x0, y0 = w2p(wx - half, wy + half)
        x1, y1 = w2p(wx + half, wy - half)
        dw, dh = x1 - x0, y1 - y0
        if (dw, dh) != bgr.shape[1::-1]:
            bgr = cv2.resize(bgr, (dw, dh), interpolation=cv2.INTER_NEAREST)
        img[y0:y0 + dh, x0:x0 + dw] = bgr
        # ring + ID label outside the marker so the bitmap stays clean
        ring = OFFPLATE_RING if off_plate else ONPLATE_RING
        cv2.rectangle(img, (x0 - 2, y0 - 2), (x0 + dw + 2, y0 + dh + 2),
                      ring, 2, cv2.LINE_AA)
        cx = (x0 + x1) // 2
        label = f"{mid} {LABELS[mid]}"
        (tw, th), _ = cv2.getTextSize(label, font, 0.55, 2)
        cv2.putText(img, label, (cx - tw // 2, y0 + dh + th + 8),
                    font, 0.55, ring, 2, cv2.LINE_AA)
        if off_plate:
            cv2.putText(img, "(off plate, coplanar w/ BT)",
                        (cx - 130, y0 - 10),
                        font, 0.45, OFFPLATE_RING, 1, cv2.LINE_AA)

    # axis arrows + labels
    org = w2p(X_MIN + 0.04, Y_MIN + 0.04)
    cv2.arrowedLine(img, org, w2p(X_MIN + 0.14, Y_MIN + 0.04),
                    AXIS, 2, cv2.LINE_AA, tipLength=0.25)
    cv2.arrowedLine(img, org, w2p(X_MIN + 0.04, Y_MIN + 0.14),
                    AXIS, 2, cv2.LINE_AA, tipLength=0.25)
    cv2.putText(img, "+X", w2p(X_MIN + 0.155, Y_MIN + 0.05),
                font, 0.55, AXIS, 2, cv2.LINE_AA)
    cv2.putText(img, "+Y", w2p(X_MIN + 0.05, Y_MIN + 0.155),
                font, 0.55, AXIS, 2, cv2.LINE_AA)

    # pitcher / catcher banners (image bottom = pitcher, image top = catcher)
    cv2.putText(img, "PITCHER side  (Y = 0)", w2p(-0.13, -0.10),
                font, 0.72, (50, 50, 50), 2, cv2.LINE_AA)
    cv2.putText(img, "CATCHER side  (Y = +0.432 m)", (W_PX // 2 - 175, 90),
                font, 0.72, (50, 50, 50), 2, cv2.LINE_AA)

    # scale bar (0.1 m) bottom-right
    bar_y = Y_MIN + 0.04
    bar_x0 = X_MAX - 0.14
    bar_x1 = bar_x0 + 0.10
    cv2.line(img, w2p(bar_x0, bar_y), w2p(bar_x1, bar_y), (40, 40, 40), 3, cv2.LINE_AA)
    cv2.line(img, w2p(bar_x0, bar_y - 0.008), w2p(bar_x0, bar_y + 0.008),
             (40, 40, 40), 3, cv2.LINE_AA)
    cv2.line(img, w2p(bar_x1, bar_y - 0.008), w2p(bar_x1, bar_y + 0.008),
             (40, 40, 40), 3, cv2.LINE_AA)
    cv2.putText(img, "10 cm", w2p(bar_x0 + 0.005, bar_y + 0.025),
                font, 0.5, (40, 40, 40), 1, cv2.LINE_AA)

    # title
    cv2.putText(img, "Home plate ArUco layout (top-down) - DICT_4X4_50 IDs 0-8",
                (20, 32), font, 0.7, TEXT, 2, cv2.LINE_AA)
    cv2.putText(img,
                f"green = on plate (0-6),  red = off plate (7,8),  marker edge = {marker_size_m*100:.1f} cm",
                (20, 56), font, 0.5, (80, 80, 80), 1, cv2.LINE_AA)

    return img


def main(out: Path, marker_size_m: float) -> int:
    img = draw(marker_size_m)
    cv2.imwrite(str(out), img)
    print(f"wrote {out}  ({img.shape[1]}x{img.shape[0]})  marker={marker_size_m*100:.1f} cm")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--marker-size-m", type=float, default=DEFAULT_MARKER_SIZE_M,
                    help="ArUco square edge length in metres (default 0.05 = 5 cm)")
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).with_name("plate_layout.png"),
                    help="output PNG path")
    args = ap.parse_args()
    raise SystemExit(main(args.out, args.marker_size_m))
