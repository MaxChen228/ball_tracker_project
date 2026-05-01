from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Trim one source video into a standalone clip by original frame range."
    )
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start-frame", type=int, required=True)
    parser.add_argument("--end-frame", type=int, required=True)
    parser.add_argument(
        "--fps",
        type=int,
        default=240,
        help="Output clip fps. Default keeps the project capture rate.",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def get_source_fps(video_path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=avg_frame_rate",
        "-of",
        "json",
        str(video_path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    rate = payload["streams"][0]["avg_frame_rate"]
    num, den = rate.split("/")
    return float(num) / float(den)


def frame_to_seconds(frame_index: int, fps: float) -> float:
    return frame_index / fps


def main() -> None:
    args = parse_args()
    if args.end_frame < args.start_frame:
        raise SystemExit("--end-frame must be >= --start-frame")
    if args.output.exists() and not args.overwrite:
        raise SystemExit(f"{args.output} exists; rerun with --overwrite to replace it")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    source_fps = get_source_fps(args.video)
    start_seconds = frame_to_seconds(args.start_frame, source_fps)
    end_seconds = frame_to_seconds(args.end_frame + 1, source_fps)
    duration_seconds = max(0.001, end_seconds - start_seconds)

    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{start_seconds:.6f}",
        "-t",
        f"{duration_seconds:.6f}",
        "-i",
        str(args.video),
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(args.fps),
        str(args.output),
    ]
    subprocess.run(cmd, check=True)

    meta = {
        "source_video": str(args.video),
        "clip_video": str(args.output),
        "source_start_frame": args.start_frame,
        "source_end_frame": args.end_frame,
        "source_fps": source_fps,
        "output_fps": args.fps,
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
        "duration_seconds": duration_seconds,
    }
    meta_path = args.output.with_suffix(args.output.suffix + ".json")
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(meta_path)


if __name__ == "__main__":
    main()
