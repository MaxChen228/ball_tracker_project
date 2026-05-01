from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from sam2.sam2_image_predictor import SAM2ImagePredictor
from sam2.sam2_video_predictor import SAM2VideoPredictor

from grounded_sam2_label_video import (
    SeedDetection,
    build_seed_mask,
    choose_device,
    propagate_with_sam2,
    render_overlay,
    update_stats,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Continue a seed-approved clip run into SAM2 propagation and overlay."
    )
    parser.add_argument("--seed-run-dir", type=Path, required=True)
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


def load_seed_run(seed_run_dir: Path) -> tuple[dict, list[int], SeedDetection]:
    stats = json.loads((seed_run_dir / "stats.json").read_text(encoding="utf-8"))
    frame_map = json.loads((seed_run_dir / "frame_map.json").read_text(encoding="utf-8"))
    seed_payload = stats["selected_seed"]
    seed = SeedDetection(
        frame_subset_index=int(seed_payload["frame_subset_index"]),
        frame_index=int(seed_payload["frame_index"]),
        image_name=str(seed_payload["image_name"]),
        label=str(seed_payload["label"]),
        score=float(seed_payload["score"]),
        box_xyxy=tuple(float(v) for v in seed_payload["box_xyxy"]),
        area_ratio=float(seed_payload["area_ratio"]),
    )
    return stats, frame_map, seed


def main() -> None:
    args = parse_args()
    stats, frame_map, seed = load_seed_run(args.seed_run_dir)
    frames_dir = args.seed_run_dir / "frames"
    previews_dir = args.seed_run_dir / "previews"
    stats_path = args.seed_run_dir / "stats.json"
    tracking_manifest_path = args.seed_run_dir / "tracking_manifest.jsonl"
    overlay_path = args.seed_run_dir / "tracking_overlay.mp4"

    device = choose_device()
    phase_start = time.perf_counter()
    image_predictor = SAM2ImagePredictor.from_pretrained(stats["sam2_model"], device=device)
    seed_mask = build_seed_mask(
        image_predictor=image_predictor,
        frames_dir=frames_dir,
        seed=seed,
        prompt_type=args.prompt_type,
        seed_mask_preview_path=previews_dir / "seed_mask.jpg",
    )
    update_stats(
        stats_path,
        seed_mask_preview=str(previews_dir / "seed_mask.jpg"),
    )
    video_predictor = SAM2VideoPredictor.from_pretrained(stats["sam2_model"], device=device)
    tracked_frames, manifest_rows = propagate_with_sam2(
        video_predictor=video_predictor,
        frames_dir=frames_dir,
        frame_map=frame_map,
        seed=seed,
        seed_mask=seed_mask,
        prompt_type=args.prompt_type,
        device=device,
        track_direction=args.track_direction,
        track_max_frames=args.track_max_frames,
        offload_video_to_cpu=args.offload_video_to_cpu,
        manifest_path=tracking_manifest_path,
    )
    update_stats(
        stats_path,
        sam2_seconds=round(time.perf_counter() - phase_start, 2),
        tracked_frames=tracked_frames,
        tracking_manifest_rows=manifest_rows,
    )

    if args.render_overlay:
        phase_start = time.perf_counter()
        render_overlay(
            manifest_path=tracking_manifest_path,
            video_path=Path(stats["video"]),
            output_path=overlay_path,
            start_frame=int(stats.get("start_frame", 0)),
            end_frame=stats.get("end_frame"),
        )
        update_stats(
            stats_path,
            overlay_seconds=round(time.perf_counter() - phase_start, 2),
            overlay_video=str(overlay_path),
        )


if __name__ == "__main__":
    main()
