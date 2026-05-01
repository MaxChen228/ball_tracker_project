from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Continue a seed-approved clip run into SAM2 propagation and overlay."
    )
    parser.add_argument("--seed-run-dir", type=Path, required=True)
    parser.add_argument(
        "--python-bin",
        default="lab/.venvs/sam2_probe/bin/python",
    )
    parser.add_argument(
        "--worker-script",
        type=Path,
        default=Path("lab/run_seeded_sam2_worker.py"),
    )
    parser.add_argument("--prompt-type", choices=("box", "mask"), default="mask")
    parser.add_argument(
        "--track-direction",
        choices=("forward", "reverse", "both"),
        default="both",
    )
    parser.add_argument("--track-max-frames", type=int, default=None)
    parser.add_argument("--offload-video-to-cpu", action="store_true")
    parser.add_argument("--render-overlay", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cmd = [
        args.python_bin,
        str(args.worker_script),
        "--seed-run-dir",
        str(args.seed_run_dir),
        "--prompt-type",
        args.prompt_type,
        "--track-direction",
        args.track_direction,
    ]
    if args.track_max_frames is not None:
        cmd.extend(["--track-max-frames", str(args.track_max_frames)])
    if args.offload_video_to_cpu:
        cmd.append("--offload-video-to-cpu")
    if args.render_overlay:
        cmd.append("--render-overlay")
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
