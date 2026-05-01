from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Grounding DINO seed selection on a pre-trimmed clip."
    )
    parser.add_argument("--clip", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--python-bin",
        default="lab/.venvs/sam2_probe/bin/python",
    )
    parser.add_argument(
        "--label-script",
        type=Path,
        default=Path("lab/grounded_sam2_label_video.py"),
    )
    parser.add_argument(
        "--text-prompt",
        default="blue ball. ball. sports ball.",
    )
    parser.add_argument("--gdino-model", default="IDEA-Research/grounding-dino-tiny")
    parser.add_argument("--sam2-model", default="facebook/sam2-hiera-tiny")
    parser.add_argument("--seed-frame-stride", type=int, default=8)
    parser.add_argument("--seed-threshold", type=float, default=0.18)
    parser.add_argument("--text-threshold", type=float, default=0.15)
    parser.add_argument("--seed-max-area-ratio", type=float, default=0.15)
    parser.add_argument("--seed-frame", type=int, default=None)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cmd = [
        args.python_bin,
        str(args.label_script),
        "--video",
        str(args.clip),
        "--output-dir",
        str(args.output_dir),
        "--text-prompt",
        args.text_prompt,
        "--gdino-model",
        args.gdino_model,
        "--sam2-model",
        args.sam2_model,
        "--seed-frame-stride",
        str(args.seed_frame_stride),
        "--seed-threshold",
        str(args.seed_threshold),
        "--text-threshold",
        str(args.text_threshold),
        "--seed-max-area-ratio",
        str(args.seed_max_area_ratio),
        "--stop-after-seed",
        "--overwrite",
    ]
    if args.seed_frame is not None:
        cmd.extend(["--seed-frame", str(args.seed_frame)])
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
