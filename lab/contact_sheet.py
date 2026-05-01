from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import av
import cv2
import numpy as np


def _probe_video(video_path: Path) -> tuple[float, int]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-count_frames",
        "-show_entries",
        "stream=nb_read_frames,avg_frame_rate",
        "-of",
        "json",
        str(video_path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    stream = json.loads(result.stdout)["streams"][0]
    num, den = stream["avg_frame_rate"].split("/")
    fps = float(num) / float(den)
    total_frames = int(stream["nb_read_frames"])
    return fps, total_frames


def _parse_cell_size(raw: str) -> tuple[int, int]:
    parts = raw.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("--cell-size must be 'W,H'")
    w = int(parts[0])
    h = int(parts[1])
    if w <= 0 or h <= 0:
        raise argparse.ArgumentTypeError("--cell-size must be positive")
    return w, h


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a contact-sheet jpg of a video for LLM-friendly skim."
    )
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--mode", choices=("macro", "micro"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tiles", type=int, choices=(9, 16, 25), default=25)
    parser.add_argument("--anchor", type=int, default=None)
    parser.add_argument("--cell-size", type=_parse_cell_size, default=(480, 270))
    return parser.parse_args()


def _target_frames(mode: str, tiles: int, anchor: int | None, total_frames: int) -> list[int]:
    if mode == "macro":
        if tiles > total_frames:
            raise SystemExit(
                f"tiles={tiles} exceeds total frames={total_frames}"
            )
        step = (total_frames - 1) / (tiles - 1)
        frames = sorted({int(round(i * step)) for i in range(tiles)})
        if len(frames) != tiles:
            raise SystemExit(
                f"could not produce {tiles} unique frame indices from total={total_frames}"
            )
        return frames
    if anchor is None:
        raise SystemExit("--anchor is required for micro mode")
    if anchor < 4 or anchor > total_frames - 5:
        raise SystemExit(
            f"anchor={anchor} out of range for total_frames={total_frames}; "
            f"need 4 <= anchor <= {total_frames - 5}"
        )
    return list(range(anchor - 4, anchor + 5))


def _decode_frames(video_path: Path, wanted: list[int]) -> dict[int, np.ndarray]:
    wanted_set = set(wanted)
    out: dict[int, np.ndarray] = {}
    container = av.open(str(video_path))
    try:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        max_wanted = max(wanted_set)
        idx = 0
        for frame in container.decode(stream):
            if idx in wanted_set:
                out[idx] = frame.to_ndarray(format="bgr24")
            idx += 1
            if idx > max_wanted:
                break
    finally:
        container.close()
    missing = wanted_set - out.keys()
    if missing:
        raise SystemExit(f"failed to decode frames: {sorted(missing)}")
    return out


def _letterbox(img: np.ndarray, cell_w: int, cell_h: int) -> np.ndarray:
    src_h, src_w = img.shape[:2]
    scale = min(cell_w / src_w, cell_h / src_h)
    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((cell_h, cell_w, 3), dtype=np.uint8)
    x0 = (cell_w - new_w) // 2
    y0 = (cell_h - new_h) // 2
    canvas[y0:y0 + new_h, x0:x0 + new_w] = resized
    return canvas


def _overlay_label(cell: np.ndarray, frame_idx: int, fps: float) -> None:
    label = f"f={frame_idx:05d} t={frame_idx / fps:.3f}s"
    box_w, box_h = 110, 20
    overlay = cell.copy()
    cv2.rectangle(overlay, (0, 0), (box_w, box_h), (0, 0, 0), thickness=-1)
    cv2.addWeighted(overlay, 0.6, cell, 0.4, 0, dst=cell)
    cv2.putText(
        cell,
        label,
        (4, 14),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )


def _compose_grid(cells: list[np.ndarray], grid_n: int, cell_w: int, cell_h: int) -> np.ndarray:
    border = 2
    sheet_w = grid_n * cell_w + (grid_n + 1) * border
    sheet_h = grid_n * cell_h + (grid_n + 1) * border
    sheet = np.full((sheet_h, sheet_w, 3), 255, dtype=np.uint8)
    for i, cell in enumerate(cells):
        r, c = divmod(i, grid_n)
        y = border + r * (cell_h + border)
        x = border + c * (cell_w + border)
        sheet[y:y + cell_h, x:x + cell_w] = cell
    return sheet


def main() -> None:
    args = parse_args()
    if not args.video.exists():
        raise SystemExit(f"video not found: {args.video}")
    fps, total_frames = _probe_video(args.video)
    if fps <= 0 or total_frames <= 0:
        raise SystemExit(f"invalid video metadata fps={fps} frames={total_frames}")

    if args.mode == "macro":
        tiles = args.tiles
    else:
        tiles = 9
    grid_n = int(round(tiles ** 0.5))

    frames = _target_frames(args.mode, tiles, args.anchor, total_frames)
    decoded = _decode_frames(args.video, frames)

    cell_w, cell_h = args.cell_size
    cells: list[np.ndarray] = []
    for idx in frames:
        cell = _letterbox(decoded[idx], cell_w, cell_h)
        _overlay_label(cell, idx, fps)
        cells.append(cell)

    sheet = _compose_grid(cells, grid_n, cell_w, cell_h)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    ok = cv2.imwrite(str(args.out), sheet, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise SystemExit(f"failed to write {args.out}")


if __name__ == "__main__":
    main()
