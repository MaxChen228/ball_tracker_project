"""SAM 2 video labeller wrapper.

Replaces the earlier `sam3_runtime.py`. SAM 3 was OOM-killing the operator's
16 GB M4 (peak ~20 GB just on weights + activation memory at image_size=1008
with 240 fps × ~2 s windows). SAM 2 hiera-tiny on MPS lands at ~1.2 GB peak
RSS and ~915 ms/frame on the same hardware — slow but offline-OK for GT.

Why **manual click prompt** instead of SAM 3's text prompt: SAM 2 has no
text encoder; it accepts only points / boxes / masks. Operator scrubs the
MOV in the /gt page, clicks the ball at the first visible frame, sends
the click image-pixel coords + frame index to the worker. Auto-seeding
via HSV detection was considered and explicitly rejected by the operator
("不要HSV自動seed，我手動", 2026-04-29) — manual is clearer about which
object SAM 2 should track when multiple HSV-positive blobs exist (e.g.
blue jacket in background).

Bridges the offline `tools/` venv (torch + transformers + Sam2VideoModel)
to the production `server/` schemas + video decoder. Loaded only by
`server/scripts/label_with_sam2.py`; production server boot never imports
this module — torch is NOT in `server/pyproject.toml`.

The on-disk GT JSON format is unchanged (still `SAM3GTRecord` /
`SAM3GTFrame` from `schemas.py`, written under `data/gt/sam3/`) so the
distillation pipeline + `validate_three_way` keep working without
migration. Only `prompt_strategy` shifts from `text:'blue ball'` to
`click:(x,y)@frame=N`.
"""
from __future__ import annotations

import datetime as _dt
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from schemas import SAM3GTFrame, SAM3GTRecord
from video import iter_frames, probe_dims

logger = logging.getLogger(__name__)


# ----- Device selection ------------------------------------------------


def _select_device(requested: str = "auto") -> str:
    """auto → cuda → mps → cpu. SAM 2 on MPS works without the SAM 3
    pin_memory monkey-patch — that bug was specific to the SAM 3 video
    processor's `batched_mask_to_box` call, which SAM 2 doesn't share."""
    if requested != "auto":
        return requested
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


# ----- Mask analysis (re-used as-is from SAM 3 era) --------------------


@dataclass
class _MaskStats:
    bbox: tuple[float, float, float, float]
    centroid_px: tuple[float, float]
    area_px: int
    aspect: float
    fill: float
    hue_mean: float
    hue_std: float
    sat_mean: float
    val_mean: float


def analyze_mask(mask: np.ndarray, bgr: np.ndarray) -> _MaskStats | None:
    """HxW mask + matching HxWx3 BGR frame → per-frame stats for fitting.

    HSV is computed on the **same H.264-decoded BGR** that server_post
    sees, so the (h, s, v) distributions can be fit directly into
    `data/hsv_range.json`. Returns None when the mask is empty."""
    if mask.dtype != np.uint8:
        mask = mask.astype(np.uint8)
    mask_bin = np.where(mask > 0, np.uint8(255), np.uint8(0))
    area = int(np.count_nonzero(mask_bin))
    if area == 0:
        return None
    ys, xs = np.where(mask_bin > 0)
    x_min, x_max = float(xs.min()), float(xs.max())
    y_min, y_max = float(ys.min()), float(ys.max())
    bbox_w = max(x_max - x_min + 1.0, 1.0)
    bbox_h = max(y_max - y_min + 1.0, 1.0)
    aspect = float(min(bbox_w, bbox_h) / max(bbox_w, bbox_h))
    fill = float(area / (bbox_w * bbox_h))
    centroid = (float(xs.mean()), float(ys.mean()))
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    hue_pixels = hsv[..., 0][mask_bin > 0]
    sat_pixels = hsv[..., 1][mask_bin > 0]
    val_pixels = hsv[..., 2][mask_bin > 0]
    return _MaskStats(
        bbox=(x_min, y_min, x_max, y_max),
        centroid_px=centroid,
        area_px=area,
        aspect=aspect,
        fill=fill,
        hue_mean=float(hue_pixels.mean()),
        hue_std=float(hue_pixels.std()),
        sat_mean=float(sat_pixels.mean()),
        val_mean=float(val_pixels.mean()),
    )


# ----- Labeller --------------------------------------------------------


class Sam2VideoLabeller:
    """SAM 2 video labeller. Lazy-loads model + processor on first
    `label_video()` call so importing this module is cheap (no torch
    graph build, no weights download). Tests stub the model attrs.

    Default checkpoint is `facebook/sam2.1-hiera-tiny` (148 MB weights,
    ~1.2 GB peak RSS on M4 MPS). Switch via `model_id`.
    """

    DEFAULT_MODEL_ID = "facebook/sam2.1-hiera-tiny"
    # Frames per chunked init_video_session. Sam2VideoProcessor.preprocess
    # resizes every frame in the batch to 1024×1024 float32 (≈12 MB
    # each) and torchvision.resize allocates ~2× that as a transient.
    # 60 frames ≈ 1.4 GB peak per chunk — comfortable on a 16 GB M4.
    # Above ~150 frames the 11 GiB ceiling we hit on 2.57 s × 240 fps
    # comes back. See `label_video` for the chunk loop.
    DEFAULT_CHUNK_SIZE = 60

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL_ID,
        device: str = "auto",
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ):
        self.model_id = model_id
        self.device = _select_device(device)
        self.chunk_size = max(1, int(chunk_size))
        self._model: Any | None = None
        self._processor: Any | None = None
        self._model_version: str | None = None

    def load(self) -> None:
        """Lazy load. Idempotent."""
        if self._model is not None and self._processor is not None:
            return
        import torch
        from transformers import Sam2VideoModel, Sam2VideoProcessor

        dtype = torch.bfloat16 if self.device != "cpu" else torch.float32
        processor = Sam2VideoProcessor.from_pretrained(self.model_id)
        model = Sam2VideoModel.from_pretrained(self.model_id)
        model = model.to(self.device, dtype=dtype)
        model.eval()
        self._model = model
        self._processor = processor
        self._model_version = (
            f"{self.model_id} (dtype={dtype}, device={self.device})"
        )
        logger.info("Sam2VideoLabeller loaded: %s", self._model_version)

    def _dtype_for_device(self) -> "Any":
        import torch
        return torch.bfloat16 if self.device != "cpu" else torch.float32

    def label_video(
        self,
        mov_path: Path,
        video_start_pts_s: float,
        session_id: str,
        camera_id: str,
        click_xy_px: tuple[int, int],
        click_t_video_rel: float,
        time_range: tuple[float, float],
        progress_callback: Callable[[int, int, float], None] | None = None,
        preview_callback: Callable[[int, np.ndarray, np.ndarray], None] | None = None,
    ) -> SAM3GTRecord:
        """Decode the MOV within `time_range` (video-relative seconds),
        seed SAM 2 with a single positive click at `click_xy_px` on the
        decoded frame nearest to `click_t_video_rel`, then propagate
        masks **forward** through the rest of the range.

        - `time_range`: (t_start, t_end) in video-relative seconds.
          Both bounds inclusive on the start, exclusive-ish on the end
          (we stop when absolute_pts_s > video_start + t_end).
        - `click_t_video_rel`: must fall within `time_range`. Defines
          the seed frame; propagation runs from that frame to range end.
          Frames before the seed get NO mask (operator should set
          range_start = click moment, but if they don't we still produce
          a partial GT for the post-click portion — better than failing).
        - `click_xy_px`: image-pixel coordinates on the source video
          (i.e., 1920×1080 typically; JS scales from CSS-px to videoWidth
          / videoHeight before POSTing).
        - Masks below `min_confidence` are NOT filtered here. SAM 2's
          mask scores are less calibrated than SAM 3's; we keep all
          propagated masks and rely on `analyze_mask` to drop empty
          ones. Distillation can re-filter by area / aspect / fill if
          needed.
        - `progress_callback(current, total, ms_per_frame)`: 1-indexed
          frame counter relative to the *seeded segment* (frames after
          the click), called after each propagation step.
        - `preview_callback(frame_idx, bgr, mask)`: synchronous; CLI
          uses this to write thumbnail JPEGs.
        """
        if not (time_range[0] <= click_t_video_rel <= time_range[1]):
            raise ValueError(
                f"click_t_video_rel={click_t_video_rel} outside time_range={time_range}"
            )

        self.load()
        assert self._model is not None and self._processor is not None

        dims = probe_dims(mov_path)
        if dims is None:
            raise RuntimeError(f"could not probe dims for {mov_path}")

        # Decode all frames in range. SAM 2's video session takes a list
        # of frames; we cache BGR for analyze_mask later.
        #
        # We ALSO track the **global MOV decode index** for each kept
        # frame so the GT JSON's `frame_idx` can be written in the same
        # index space as `pitch.frames_server_post[i].frame_index`.
        # validate_three_way + distill_all key both off frame_idx for
        # cross-source alignment; if GT used a time_range-local index
        # the matching would silently miss every frame on the SAM 2
        # side. Consumed in the gt_frames append below.
        t_start_abs = video_start_pts_s + time_range[0]
        t_end_abs = video_start_pts_s + time_range[1]
        t_click_abs = video_start_pts_s + click_t_video_rel

        frame_bgrs: list[np.ndarray] = []
        frame_pts: list[float] = []
        frame_global_idx: list[int] = []
        global_idx = 0
        for absolute_pts_s, bgr in iter_frames(mov_path, video_start_pts_s):
            in_range = (absolute_pts_s >= t_start_abs and absolute_pts_s <= t_end_abs)
            if in_range:
                frame_bgrs.append(bgr)
                frame_pts.append(absolute_pts_s)
                frame_global_idx.append(global_idx)
            elif absolute_pts_s > t_end_abs:
                break
            global_idx += 1

        if not frame_bgrs:
            raise RuntimeError(
                f"no decodable frames in {mov_path} within time_range {time_range}"
            )

        # Find seed frame: the one closest in PTS to t_click_abs.
        seed_idx = int(np.argmin([abs(p - t_click_abs) for p in frame_pts]))
        logger.info(
            "decoded %d frames in range; seed at in_range_idx=%d global_idx=%d "
            "(pts=%.3f, click=%.3f)",
            len(frame_bgrs), seed_idx, frame_global_idx[seed_idx],
            frame_pts[seed_idx], t_click_abs,
        )

        video_fps = (
            (len(frame_pts) - 1) / (frame_pts[-1] - frame_pts[0])
            if len(frame_pts) > 1 and frame_pts[-1] > frame_pts[0]
            else 0.0
        )

        H, W = frame_bgrs[0].shape[:2]
        click_x, click_y = int(click_xy_px[0]), int(click_xy_px[1])
        if not (0 <= click_x < W and 0 <= click_y < H):
            raise ValueError(
                f"click_xy_px=({click_x},{click_y}) outside frame dims {W}x{H}"
            )

        # ----- chunked propagation ------------------------------------
        # Sam2VideoProcessor batches the entire input video into a single
        # 1024×1024 float32 tensor and torchvision.resize allocates ~2×
        # the resized size as a transient. For 617 frames at 1080p that
        # blew up to 11 GiB on the 16 GB M4 (smoke-test on s_4b23a195
        # 2026-04-29). We chunk into `self.chunk_size`-frame windows and
        # carry SAM 2 state across chunks via a re-seed at the centroid
        # of the previous chunk's last non-empty mask. Chunk boundaries
        # lose the SAM 2 memory bank, so a small (60-frame) window keeps
        # the mask-prompted re-seed close to the last frame's geometry.
        #
        # Why centroid (point) re-seed, not raw `input_masks`:
        #   add_inputs_to_inference_session accepts input_masks tensors,
        #   but the expected internal shape isn't documented stably and
        #   we'd be coupling to a transformers main-branch implementation
        #   detail. Centroid is robust, easy to debug, matches what we
        #   show on the preview thumbnail, and is good enough for a ball
        #   (~50 px object — point prompts work well).
        gt_frames: list[SAM3GTFrame] = []
        total_to_track = len(frame_bgrs) - seed_idx
        ema_ms_per_frame: float | None = None
        last_tick = time.monotonic()
        n_propagated = 0
        carry_seed_xy: tuple[int, int] = (click_x, click_y)
        chunk_idx = 0

        chunk_start = seed_idx
        while chunk_start < len(frame_bgrs):
            chunk_end = min(chunk_start + self.chunk_size, len(frame_bgrs))
            chunk_rgbs = [
                cv2.cvtColor(b, cv2.COLOR_BGR2RGB)
                for b in frame_bgrs[chunk_start:chunk_end]
            ]
            logger.info(
                "chunk %d: frames %d..%d (%d), seed=(%d,%d)",
                chunk_idx, chunk_start, chunk_end - 1,
                len(chunk_rgbs), carry_seed_xy[0], carry_seed_xy[1],
            )

            sess = self._processor.init_video_session(
                video=chunk_rgbs,
                inference_device=self.device,
                dtype=self._dtype_for_device(),
            )
            # Always seed at chunk-local frame 0:
            #   chunk 0 → operator's click point
            #   chunk 1+ → centroid of previous chunk's last good mask
            self._processor.add_inputs_to_inference_session(
                inference_session=sess,
                frame_idx=0,
                obj_ids=1,
                input_points=[[[[carry_seed_xy[0], carry_seed_xy[1]]]]],
                input_labels=[[[1]]],
            )

            chunk_last_centroid: tuple[int, int] | None = None
            for sam2_out in self._model.propagate_in_video_iterator(
                inference_session=sess,
                start_frame_idx=0,
            ):
                n_propagated += 1
                now = time.monotonic()
                sample_ms = (now - last_tick) * 1000.0
                last_tick = now
                if ema_ms_per_frame is None:
                    ema_ms_per_frame = sample_ms
                else:
                    ema_ms_per_frame = 0.7 * ema_ms_per_frame + 0.3 * sample_ms

                masks_list = self._processor.post_process_masks(
                    [sam2_out.pred_masks], original_sizes=[[H, W]]
                )
                import torch  # local: inside hot loop, but cached after first use
                mask_np = masks_list[0][0, 0].detach().to("cpu").to(torch.uint8).numpy()
                mask_u8 = np.where(mask_np > 0, np.uint8(255), np.uint8(0))

                # sam2_out.frame_idx is local to the chunk's input list.
                # Map back to our `frame_bgrs` index, then to the MOV
                # global decode index for the GT JSON's frame_idx field.
                local_idx = int(sam2_out.frame_idx)
                in_range_idx = chunk_start + local_idx
                absolute_idx = frame_global_idx[in_range_idx]
                stats = analyze_mask(mask_u8, frame_bgrs[in_range_idx])
                if stats is None:
                    # Empty mask — SAM 2 lost the object on this frame.
                    # Don't update carry_seed_xy from this; if the next
                    # chunk inherits, we re-use the previous good
                    # centroid (or the original click on first chunk).
                    if progress_callback is not None:
                        progress_callback(n_propagated, total_to_track, ema_ms_per_frame)
                    continue

                gt_frames.append(SAM3GTFrame(
                    frame_idx=absolute_idx,
                    t_pts_s=frame_pts[in_range_idx],
                    bbox=stats.bbox,
                    centroid_px=stats.centroid_px,
                    mask_area_px=stats.area_px,
                    mask_aspect=stats.aspect,
                    mask_fill=stats.fill,
                    mask_hue_mean=stats.hue_mean,
                    mask_hue_std=stats.hue_std,
                    mask_sat_mean=stats.sat_mean,
                    mask_val_mean=stats.val_mean,
                    confidence=1.0,
                ))
                # Update carry centroid from THIS frame's mask. The last
                # iteration's value is what feeds chunk N+1.
                chunk_last_centroid = (
                    int(stats.centroid_px[0]), int(stats.centroid_px[1])
                )
                if preview_callback is not None:
                    try:
                        preview_callback(absolute_idx, frame_bgrs[in_range_idx], mask_u8)
                    except Exception as e:
                        logger.warning("preview_callback raised: %s", e)
                if progress_callback is not None:
                    progress_callback(n_propagated, total_to_track, ema_ms_per_frame)

            # Decide next chunk's seed. If we never produced a non-empty
            # mask in this chunk, propagation has lost the object — stop
            # rather than feed the same stale seed forward forever.
            if chunk_last_centroid is None:
                logger.warning(
                    "chunk %d produced no masks — stopping propagation at frame %d",
                    chunk_idx, chunk_end - 1,
                )
                break
            carry_seed_xy = chunk_last_centroid
            chunk_idx += 1
            chunk_start = chunk_end

            # Free the chunk-scope batch + session. The model + processor
            # stay loaded for the next chunk.
            del sess
            del chunk_rgbs

        gt_frames.sort(key=lambda f: f.frame_idx)
        return SAM3GTRecord(
            session_id=session_id,
            camera_id=camera_id,
            model_version=self._model_version or self.model_id,
            labelled_at=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            prompt_strategy=(
                f"click:({click_x},{click_y})@global_frame={frame_global_idx[seed_idx]}"
                f" chunked@{self.chunk_size} centroid-reseed"
            ),
            video_fps=video_fps,
            video_dims=dims,
            frames=gt_frames,
            frames_decoded=len(frame_bgrs),
            frames_labelled=len(gt_frames),
            min_confidence=0.0,  # not applicable to SAM 2 single-click track
        )
