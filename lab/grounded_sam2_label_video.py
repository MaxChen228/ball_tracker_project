from __future__ import annotations

import argparse
import json
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import av
import cv2
import numpy as np
import torch
from PIL import Image
from tqdm import tqdm
from sam2.sam2_image_predictor import SAM2ImagePredictor
from sam2.sam2_video_predictor import SAM2VideoPredictor
from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor


@dataclass(frozen=True)
class SeedDetection:
    frame_subset_index: int
    frame_index: int
    image_name: str
    label: str
    score: float
    box_xyxy: tuple[float, float, float, float]
    area_ratio: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ground videos with Grounding DINO, then propagate with SAM 2."
    )
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--text-prompt",
        default="blue ball. ball. sports ball.",
        help="Use period-separated phrases for Grounding DINO.",
    )
    parser.add_argument(
        "--gdino-model",
        default="IDEA-Research/grounding-dino-tiny",
    )
    parser.add_argument(
        "--sam2-model",
        default="facebook/sam2-hiera-tiny",
    )
    parser.add_argument("--start-frame", type=int, default=0)
    parser.add_argument("--end-frame", type=int, default=None)
    parser.add_argument(
        "--seed-frame-stride",
        type=int,
        default=16,
        help="Only run Grounding DINO every Nth extracted frame.",
    )
    parser.add_argument("--seed-threshold", type=float, default=0.18)
    parser.add_argument("--text-threshold", type=float, default=0.15)
    parser.add_argument(
        "--seed-max-area-ratio",
        type=float,
        default=0.15,
        help="Reject giant hallucination boxes during seed search.",
    )
    parser.add_argument(
        "--seed-frame",
        type=int,
        default=None,
        help="Original frame index override for the SAM2 seed.",
    )
    parser.add_argument(
        "--prompt-type",
        choices=("box", "mask"),
        default="mask",
    )
    parser.add_argument(
        "--track-direction",
        choices=("forward", "reverse", "both"),
        default="both",
    )
    parser.add_argument(
        "--track-max-frames",
        type=int,
        default=None,
        help="Optional cap for each propagation direction.",
    )
    parser.add_argument(
        "--offload-video-to-cpu",
        action="store_true",
        help="Reduce MPS memory pressure during SAM2 video init.",
    )
    parser.add_argument(
        "--save-seed-candidates",
        type=int,
        default=20,
        help="How many top seed rows to save in stats.json.",
    )
    parser.add_argument(
        "--stop-after-seed",
        action="store_true",
        help="Exit after Grounding DINO seed selection and previews.",
    )
    parser.add_argument(
        "--render-overlay",
        action="store_true",
        help="Render tracked boxes back onto the source video window.",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def choose_device() -> str:
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def ensure_output_dir(output_dir: Path, overwrite: bool) -> None:
    if output_dir.exists():
        if not overwrite:
            raise SystemExit(
                f"{output_dir} exists; rerun with --overwrite to replace it"
            )
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def update_stats(stats_path: Path, **fields: object) -> None:
    if stats_path.exists():
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
    else:
        stats = {}
    stats.update(fields)
    write_json(stats_path, stats)


def extract_frames(
    *,
    video_path: Path,
    frames_dir: Path,
    frame_map_path: Path,
    start_frame: int,
    end_frame: int | None,
) -> list[int]:
    container = av.open(str(video_path))
    stream = container.streams.video[0]
    total = stream.frames or None
    frame_map: list[int] = []
    progress = tqdm(
        total=total,
        desc="extract frames",
        unit="frame",
    )
    try:
        subset_index = 0
        for frame_index, frame in enumerate(container.decode(video=0)):
            progress.update(1)
            if frame_index < start_frame:
                continue
            if end_frame is not None and frame_index > end_frame:
                break
            image = frame.to_ndarray(format="bgr24")
            image_name = f"{subset_index:05d}.jpg"
            cv2.imwrite(str(frames_dir / image_name), image)
            frame_map.append(frame_index)
            subset_index += 1
    finally:
        progress.close()
        container.close()
    frame_map_path.write_text(json.dumps(frame_map, indent=2), encoding="utf-8")
    return frame_map


def load_grounding_models(
    model_id: str, device: str
) -> tuple[AutoProcessor, AutoModelForZeroShotObjectDetection]:
    processor = AutoProcessor.from_pretrained(model_id)
    model = AutoModelForZeroShotObjectDetection.from_pretrained(model_id).to(device)
    model.eval()
    return processor, model


def rank_seed_key(seed: SeedDetection) -> tuple[float, float]:
    # Bias hard toward tiny high-confidence boxes; this is a small-ball pipeline,
    # so large "blue region" detections should lose even if their raw score is
    # slightly higher.
    adjusted_score = seed.score - (seed.area_ratio * 5.0)
    return (adjusted_score, seed.score)


def scan_seed_candidates(
    *,
    frames_dir: Path,
    frame_map: list[int],
    processor: AutoProcessor,
    grounding_model: AutoModelForZeroShotObjectDetection,
    text_prompt: str,
    device: str,
    stride: int,
    threshold: float,
    text_threshold: float,
    seed_max_area_ratio: float,
    manifest_path: Path,
) -> list[SeedDetection]:
    candidates: list[SeedDetection] = []
    frame_paths = sorted(frames_dir.glob("*.jpg"))
    with manifest_path.open("w", encoding="utf-8") as manifest_fh:
        for subset_index, frame_path in enumerate(
            tqdm(frame_paths, desc="grounding seeds", unit="frame")
        ):
            if subset_index % stride != 0:
                continue
            image = Image.open(frame_path).convert("RGB")
            inputs = processor(
                images=image,
                text=text_prompt,
                return_tensors="pt",
            ).to(device)
            with torch.no_grad():
                outputs = grounding_model(**inputs)
            result = processor.post_process_grounded_object_detection(
                outputs,
                inputs.input_ids,
                threshold=threshold,
                text_threshold=text_threshold,
                target_sizes=[image.size[::-1]],
            )[0]
            width, height = image.size
            image_area = float(width * height)
            rows: list[dict] = []
            labels = result.get("text_labels", result["labels"])
            for box, score, label in zip(result["boxes"], result["scores"], labels):
                x1, y1, x2, y2 = [float(x) for x in box.tolist()]
                area_ratio = max(0.0, ((x2 - x1) * (y2 - y1)) / image_area)
                row = {
                    "frame_subset_index": subset_index,
                    "frame_index": frame_map[subset_index],
                    "image_name": frame_path.name,
                    "label": str(label),
                    "score": float(score.item()),
                    "box_xyxy": [x1, y1, x2, y2],
                    "area_ratio": area_ratio,
                }
                rows.append(row)
                if area_ratio > seed_max_area_ratio:
                    continue
                candidates.append(
                    SeedDetection(
                        frame_subset_index=subset_index,
                        frame_index=frame_map[subset_index],
                        image_name=frame_path.name,
                        label=str(label),
                        score=float(score.item()),
                        box_xyxy=(x1, y1, x2, y2),
                        area_ratio=area_ratio,
                    )
                )
            manifest_fh.write(
                json.dumps(
                    {
                        "frame_subset_index": subset_index,
                        "frame_index": frame_map[subset_index],
                        "image_name": frame_path.name,
                        "detections": rows,
                    }
                )
                + "\n"
            )
            manifest_fh.flush()
    candidates.sort(key=rank_seed_key, reverse=True)
    return candidates


def choose_seed(
    candidates: list[SeedDetection],
    frame_map: list[int],
    override_frame_index: int | None,
) -> SeedDetection:
    if override_frame_index is not None:
        for candidate in candidates:
            if candidate.frame_index == override_frame_index:
                return candidate
        raise SystemExit(
            f"--seed-frame={override_frame_index} was not found among seed candidates"
        )
    if not candidates:
        raise SystemExit("no seed detections survived grounding scan")
    return candidates[0]


def save_seed_preview(
    *,
    frames_dir: Path,
    seed: SeedDetection,
    seed_preview_path: Path,
) -> np.ndarray:
    image = cv2.imread(str(frames_dir / seed.image_name))
    if image is None:
        raise SystemExit(f"failed to read seed image {frames_dir / seed.image_name}")
    x1, y1, x2, y2 = [int(round(v)) for v in seed.box_xyxy]
    cv2.rectangle(image, (x1, y1), (x2, y2), (0, 255, 255), 3)
    cv2.putText(
        image,
        f"{seed.label} {seed.score:.3f}",
        (x1, max(30, y1 - 10)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (0, 255, 255),
        2,
        cv2.LINE_AA,
    )
    cv2.imwrite(str(seed_preview_path), image)
    return image


def mask_to_xyxy(mask: np.ndarray) -> tuple[float, float, float, float] | None:
    ys, xs = np.where(mask > 0)
    if len(xs) == 0 or len(ys) == 0:
        return None
    return (
        float(xs.min()),
        float(ys.min()),
        float(xs.max()),
        float(ys.max()),
    )


def build_seed_mask(
    *,
    image_predictor: SAM2ImagePredictor,
    frames_dir: Path,
    seed: SeedDetection,
    prompt_type: str,
    seed_mask_preview_path: Path,
) -> np.ndarray | None:
    image = np.array(Image.open(frames_dir / seed.image_name).convert("RGB"))
    image_predictor.set_image(image)
    input_box = np.array([seed.box_xyxy], dtype=np.float32)
    masks, scores, _ = image_predictor.predict(
        point_coords=None,
        point_labels=None,
        box=input_box,
        multimask_output=False,
    )
    if masks.ndim == 4:
        masks = masks.squeeze(1)
    if masks.ndim != 3 or masks.shape[0] == 0:
        raise SystemExit("SAM2 image predictor returned no masks for the seed box")
    best_idx = int(np.argmax(scores))
    seed_mask = masks[best_idx].astype(np.uint8)
    preview = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    overlay = preview.copy()
    overlay[seed_mask > 0] = (0, 255, 255)
    preview = cv2.addWeighted(preview, 0.7, overlay, 0.3, 0)
    x1, y1, x2, y2 = [int(round(v)) for v in seed.box_xyxy]
    cv2.rectangle(preview, (x1, y1), (x2, y2), (0, 255, 255), 2)
    cv2.putText(
        preview,
        f"seed mask score={float(scores[best_idx]):.3f}",
        (20, 40),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    cv2.imwrite(str(seed_mask_preview_path), preview)
    if prompt_type == "box":
        return None
    return seed_mask


def propagate_with_sam2(
    *,
    video_predictor: SAM2VideoPredictor,
    frames_dir: Path,
    frame_map: list[int],
    seed: SeedDetection,
    seed_mask: np.ndarray | None,
    prompt_type: str,
    device: str,
    track_direction: str,
    track_max_frames: int | None,
    offload_video_to_cpu: bool,
    manifest_path: Path,
) -> tuple[int, int]:
    inference_state = video_predictor.init_state(
        video_path=str(frames_dir),
        offload_video_to_cpu=offload_video_to_cpu,
    )
    if prompt_type == "mask":
        if seed_mask is None:
            raise SystemExit("mask prompt requested, but seed mask is missing")
        video_predictor.add_new_mask(
            inference_state=inference_state,
            frame_idx=seed.frame_subset_index,
            obj_id=1,
            mask=seed_mask,
        )
    else:
        video_predictor.add_new_points_or_box(
            inference_state=inference_state,
            frame_idx=seed.frame_subset_index,
            obj_id=1,
            box=np.array(seed.box_xyxy, dtype=np.float32),
        )

    tracked_rows: dict[int, dict] = {}
    for reverse in directions_for(track_direction):
        for out_frame_idx, out_obj_ids, out_mask_logits in video_predictor.propagate_in_video(
            inference_state,
            start_frame_idx=seed.frame_subset_index,
            max_frame_num_to_track=track_max_frames,
            reverse=reverse,
        ):
            del out_obj_ids
            mask = (out_mask_logits[0] > 0.0).detach().cpu().numpy().astype(np.uint8)
            if mask.ndim == 3:
                mask = mask.squeeze(0)
            xyxy = mask_to_xyxy(mask)
            if xyxy is None:
                continue
            tracked_rows[out_frame_idx] = {
                "frame_subset_index": out_frame_idx,
                "frame_index": frame_map[out_frame_idx],
                "image_name": f"{out_frame_idx:05d}.jpg",
                "source": "sam2",
                "seed_frame_index": seed.frame_index,
                "detections": [
                    {
                        "label": seed.label,
                        "conf": 1.0,
                        "xyxy": [float(v) for v in xyxy],
                        "mask_area_px": int(mask.sum()),
                    }
                ],
            }
            flush_tracking_rows(manifest_path, tracked_rows)
    flush_tracking_rows(manifest_path, tracked_rows)
    return len(tracked_rows), len(tracked_rows)


def directions_for(track_direction: str) -> list[bool]:
    if track_direction == "forward":
        return [False]
    if track_direction == "reverse":
        return [True]
    return [False, True]


def flush_tracking_rows(manifest_path: Path, tracked_rows: dict[int, dict]) -> None:
    ordered = [tracked_rows[idx] for idx in sorted(tracked_rows)]
    with manifest_path.open("w", encoding="utf-8") as fh:
        for row in ordered:
            fh.write(json.dumps(row) + "\n")


def render_overlay(
    *,
    manifest_path: Path,
    video_path: Path,
    output_path: Path,
    start_frame: int,
    end_frame: int | None,
) -> None:
    rows_by_frame: dict[int, list[dict]] = {}
    with manifest_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            rows_by_frame[int(row["frame_index"])] = row["detections"]

    container = av.open(str(video_path))
    stream = container.streams.video[0]
    width = stream.codec_context.width
    height = stream.codec_context.height
    fps_raw = float(stream.average_rate) if stream.average_rate else 30.0
    fps = max(1.0, round(fps_raw))
    writer = cv2.VideoWriter(
        str(output_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    if not writer.isOpened():
        raise SystemExit(f"failed to open writer for {output_path}")
    progress = tqdm(total=stream.frames or None, desc="render overlay", unit="frame")
    try:
        for frame_index, frame in enumerate(container.decode(video=0)):
            progress.update(1)
            if frame_index < start_frame:
                continue
            if end_frame is not None and frame_index > end_frame:
                break
            image = frame.to_ndarray(format="bgr24")
            detections = rows_by_frame.get(frame_index, [])
            for det in detections:
                x1, y1, x2, y2 = [int(round(v)) for v in det["xyxy"]]
                cv2.rectangle(image, (x1, y1), (x2, y2), (0, 255, 255), 2)
                cv2.putText(
                    image,
                    det.get("label", "ball"),
                    (x1, max(24, y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (0, 255, 255),
                    2,
                    cv2.LINE_AA,
                )
            cv2.putText(
                image,
                f"{video_path.name} frame={frame_index}",
                (20, 36),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )
            writer.write(image)
    finally:
        progress.close()
        writer.release()
        container.close()


def main() -> None:
    args = parse_args()
    ensure_output_dir(args.output_dir, args.overwrite)

    frames_dir = args.output_dir / "frames"
    previews_dir = args.output_dir / "previews"
    frames_dir.mkdir()
    previews_dir.mkdir()

    stats_path = args.output_dir / "stats.json"
    frame_map_path = args.output_dir / "frame_map.json"
    seed_manifest_path = args.output_dir / "seed_candidates.jsonl"
    tracking_manifest_path = args.output_dir / "tracking_manifest.jsonl"

    update_stats(
        stats_path,
        video=str(args.video),
        text_prompt=args.text_prompt,
        gdino_model=args.gdino_model,
        sam2_model=args.sam2_model,
        device=choose_device(),
        start_frame=args.start_frame,
        end_frame=args.end_frame,
    )

    phase_start = time.perf_counter()
    frame_map = extract_frames(
        video_path=args.video,
        frames_dir=frames_dir,
        frame_map_path=frame_map_path,
        start_frame=args.start_frame,
        end_frame=args.end_frame,
    )
    update_stats(
        stats_path,
        frames_extracted=len(frame_map),
        extract_seconds=round(time.perf_counter() - phase_start, 2),
    )

    device = choose_device()
    phase_start = time.perf_counter()
    processor, grounding_model = load_grounding_models(args.gdino_model, device)
    candidates = scan_seed_candidates(
        frames_dir=frames_dir,
        frame_map=frame_map,
        processor=processor,
        grounding_model=grounding_model,
        text_prompt=args.text_prompt,
        device=device,
        stride=args.seed_frame_stride,
        threshold=args.seed_threshold,
        text_threshold=args.text_threshold,
        seed_max_area_ratio=args.seed_max_area_ratio,
        manifest_path=seed_manifest_path,
    )
    seed = choose_seed(candidates, frame_map, args.seed_frame)
    update_stats(
        stats_path,
        seed_scan_seconds=round(time.perf_counter() - phase_start, 2),
        seed_candidates=len(candidates),
        top_seed_candidates=[
            asdict(candidate) for candidate in candidates[: args.save_seed_candidates]
        ],
        selected_seed=asdict(seed),
    )

    save_seed_preview(
        frames_dir=frames_dir,
        seed=seed,
        seed_preview_path=previews_dir / "seed_grounding.jpg",
    )
    update_stats(
        stats_path,
        seed_grounding_preview=str(previews_dir / "seed_grounding.jpg"),
    )
    if args.stop_after_seed:
        return

    phase_start = time.perf_counter()
    image_predictor = SAM2ImagePredictor.from_pretrained(args.sam2_model, device=device)
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
    video_predictor = SAM2VideoPredictor.from_pretrained(args.sam2_model, device=device)
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
            video_path=args.video,
            output_path=args.output_dir / "tracking_overlay.mp4",
            start_frame=args.start_frame,
            end_frame=args.end_frame,
        )
        update_stats(
            stats_path,
            overlay_seconds=round(time.perf_counter() - phase_start, 2),
            overlay_video=str(args.output_dir / "tracking_overlay.mp4"),
        )


if __name__ == "__main__":
    main()
