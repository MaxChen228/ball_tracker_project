"""ChArUco camera-intrinsic calibration script.

Usage:
  uv run python calibrate_intrinsics.py \
    --images-glob 'calib/*.jpg' \
    --squares-x 5 --squares-y 7 \
    --square-length-m 0.040 --marker-length-m 0.030 \
    --dict 4X4_50 \
    --out intrinsics.json

Also supports:
  uv run python calibrate_intrinsics.py --print-board --out board.png
    → writes a ChArUco board PNG you can print on A4 (at 1:1 scale!).

The resulting intrinsics.json holds fx/fy/cx/cy that can be pasted into the
iOS app's Settings → Manual Intrinsics fields to replace the FOV approximation
that the app otherwise derives from AVCaptureDevice.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

ARUCO_DICTS = {
    "4X4_50": cv2.aruco.DICT_4X4_50,
    "4X4_100": cv2.aruco.DICT_4X4_100,
    "4X4_250": cv2.aruco.DICT_4X4_250,
    "5X5_50": cv2.aruco.DICT_5X5_50,
    "5X5_100": cv2.aruco.DICT_5X5_100,
    "6X6_50": cv2.aruco.DICT_6X6_50,
}


@dataclass
class BoardSpec:
    squares_x: int
    squares_y: int
    square_length_m: float
    marker_length_m: float
    dict_name: str = "4X4_50"

    def build(self) -> tuple[cv2.aruco.CharucoBoard, cv2.aruco.Dictionary]:
        if self.dict_name not in ARUCO_DICTS:
            raise SystemExit(f"unknown --dict {self.dict_name!r}; choose from {list(ARUCO_DICTS)}")
        if self.marker_length_m >= self.square_length_m:
            raise SystemExit("marker must be smaller than square (marker sits inside square)")
        aruco_dict = cv2.aruco.getPredefinedDictionary(ARUCO_DICTS[self.dict_name])
        board = cv2.aruco.CharucoBoard(
            (self.squares_x, self.squares_y),
            self.square_length_m,
            self.marker_length_m,
            aruco_dict,
        )
        return board, aruco_dict


@dataclass
class CalibrationResult:
    fx: float
    fy: float
    cx: float
    cy: float
    image_width: int
    image_height: int
    rms_reprojection_error_px: float
    distortion_coeffs: list[float]
    num_images_used: int
    image_paths_used: list[str] = field(default_factory=list)

    def to_json(self, board: BoardSpec) -> dict:
        return {
            "fx": self.fx,
            "fy": self.fy,
            "cx": self.cx,
            "cy": self.cy,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "rms_reprojection_error_px": self.rms_reprojection_error_px,
            "distortion_coeffs": self.distortion_coeffs,
            "num_images_used": self.num_images_used,
            "image_paths_used": self.image_paths_used,
            "board": {
                "squares_x": board.squares_x,
                "squares_y": board.squares_y,
                "square_length_m": board.square_length_m,
                "marker_length_m": board.marker_length_m,
                "dict": board.dict_name,
            },
        }


def render_board_png(board_spec: BoardSpec, out_path: Path, pixels_per_m: int = 4000) -> None:
    """Render the ChArUco board at scale so an A4-sized image prints 1:1."""
    board, _ = board_spec.build()
    w_px = int(board_spec.squares_x * board_spec.square_length_m * pixels_per_m)
    h_px = int(board_spec.squares_y * board_spec.square_length_m * pixels_per_m)
    img = board.generateImage((w_px, h_px), marginSize=20, borderBits=1)
    if not cv2.imwrite(str(out_path), img):
        raise SystemExit(f"failed to write {out_path}")


def calibrate_from_images(
    image_paths: list[str],
    board_spec: BoardSpec,
    min_corners_per_image: int = 6,
) -> CalibrationResult:
    if not image_paths:
        raise SystemExit("no input images matched --images-glob")

    board, _ = board_spec.build()
    detector = cv2.aruco.CharucoDetector(board)

    all_corners: list[np.ndarray] = []
    all_ids: list[np.ndarray] = []
    used_paths: list[str] = []
    image_size: tuple[int, int] | None = None

    for path in image_paths:
        gray = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if gray is None:
            print(f"[skip] cannot read {path}", file=sys.stderr)
            continue
        if image_size is None:
            image_size = (gray.shape[1], gray.shape[0])
        elif (gray.shape[1], gray.shape[0]) != image_size:
            print(f"[skip] {path}: size mismatch ({gray.shape[1]}x{gray.shape[0]} vs {image_size[0]}x{image_size[1]})", file=sys.stderr)
            continue

        charuco_corners, charuco_ids, _, _ = detector.detectBoard(gray)
        if charuco_corners is None or charuco_ids is None:
            print(f"[skip] {path}: no board detected", file=sys.stderr)
            continue
        if len(charuco_corners) < min_corners_per_image:
            print(f"[skip] {path}: only {len(charuco_corners)} corners (< {min_corners_per_image})", file=sys.stderr)
            continue

        all_corners.append(charuco_corners)
        all_ids.append(charuco_ids)
        used_paths.append(path)

    if len(all_corners) < 4:
        raise SystemExit(
            f"only {len(all_corners)} usable image(s); need ≥4 with varied poses for a stable K"
        )
    assert image_size is not None

    flags = 0
    rms, K, dist, _, _ = cv2.aruco.calibrateCameraCharuco(
        all_corners,
        all_ids,
        board,
        image_size,
        cameraMatrix=None,
        distCoeffs=None,
        flags=flags,
    )

    return CalibrationResult(
        fx=float(K[0, 0]),
        fy=float(K[1, 1]),
        cx=float(K[0, 2]),
        cy=float(K[1, 2]),
        image_width=image_size[0],
        image_height=image_size[1],
        rms_reprojection_error_px=float(rms),
        distortion_coeffs=[float(x) for x in dist.flatten().tolist()],
        num_images_used=len(all_corners),
        image_paths_used=used_paths,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--images-glob", help="glob of calibration images, e.g. 'calib/*.jpg'")
    ap.add_argument("--squares-x", type=int, default=5, help="checker squares horizontally")
    ap.add_argument("--squares-y", type=int, default=7, help="checker squares vertically")
    ap.add_argument("--square-length-m", type=float, default=0.040, help="square size in meters (measure your printout!)")
    ap.add_argument("--marker-length-m", type=float, default=0.030, help="marker size in meters (must be < square)")
    ap.add_argument("--dict", default="4X4_50", help=f"ArUco dict; one of {list(ARUCO_DICTS)}")
    ap.add_argument("--min-corners-per-image", type=int, default=6, help="minimum charuco corners to accept an image")
    ap.add_argument("--out", type=Path, help="output path (.json for calibration, .png for board)")
    ap.add_argument("--print-board", action="store_true", help="render the board as PNG and exit")
    args = ap.parse_args(argv)

    board_spec = BoardSpec(
        squares_x=args.squares_x,
        squares_y=args.squares_y,
        square_length_m=args.square_length_m,
        marker_length_m=args.marker_length_m,
        dict_name=args.dict,
    )

    if args.print_board:
        if not args.out:
            raise SystemExit("--print-board requires --out path.png")
        render_board_png(board_spec, args.out)
        print(f"wrote {args.out}  ({board_spec.squares_x}x{board_spec.squares_y} squares, "
              f"{board_spec.square_length_m*100:.1f}cm squares, {board_spec.marker_length_m*100:.1f}cm markers)")
        return 0

    if not args.images_glob:
        raise SystemExit("need --images-glob <pattern> (or --print-board)")
    if not args.out:
        raise SystemExit("need --out intrinsics.json")

    paths = sorted(glob.glob(args.images_glob))
    result = calibrate_from_images(paths, board_spec, args.min_corners_per_image)

    args.out.write_text(json.dumps(result.to_json(board_spec), indent=2))
    print(
        f"calibrated from {result.num_images_used}/{len(paths)} images "
        f"(rms={result.rms_reprojection_error_px:.3f}px): "
        f"fx={result.fx:.1f}  fy={result.fy:.1f}  cx={result.cx:.1f}  cy={result.cy:.1f}"
    )
    print(f"→ {args.out}")
    print("Paste fx/fy/cx/cy into iOS Settings → Manual Intrinsics (app uses fz ≡ fy).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
