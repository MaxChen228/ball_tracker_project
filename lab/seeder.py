from __future__ import annotations

import io
import os
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

if TYPE_CHECKING:
    from sam2.sam2_image_predictor import SAM2ImagePredictor


def _pick_device() -> str:
    import torch

    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


class Seeder:
    """Holds a SAM 2 image predictor in memory and turns (frame, point) into a PNG mask."""

    def __init__(self, model_id: str | None = None) -> None:
        from sam2.sam2_image_predictor import SAM2ImagePredictor

        if model_id is None:
            model_id = os.environ.get("SAM2_IMAGE_MODEL", "facebook/sam2-hiera-large")
        self.device = _pick_device()
        self.model_id = model_id
        self._predictor: SAM2ImagePredictor = SAM2ImagePredictor.from_pretrained(
            model_id, device=self.device
        )
        self._lock = Lock()

    def seed_at(self, frame_bgr: np.ndarray, x: int, y: int) -> bytes:
        """Run a single positive-point prompt; return mask PNG bytes (binary, 0/255).

        SAM 2 returns 3 hierarchical masks for an ambiguous single point. For
        small objects on textured backgrounds the highest-scored one is often
        the surrounding region, not the object. We pick the smallest mask
        whose area > 0 — i.e. the tightest interpretation of the click.
        """
        rgb = frame_bgr[:, :, ::-1].copy()
        with self._lock:
            self._predictor.set_image(rgb)
            masks, scores, _ = self._predictor.predict(
                point_coords=np.array([[x, y]], dtype=np.float32),
                point_labels=np.array([1], dtype=np.int32),
                multimask_output=True,
            )
        areas = masks.reshape(masks.shape[0], -1).sum(axis=1)
        valid = np.where(areas > 0)[0]
        if valid.size == 0:
            raise RuntimeError("SAM 2 returned all-zero masks for this click")
        pick = int(valid[np.argmin(areas[valid])])
        mask = masks[pick].astype(np.uint8) * 255
        buf = io.BytesIO()
        Image.fromarray(mask, mode="L").save(buf, format="PNG", optimize=False)
        return buf.getvalue()

    def write_png(self, mask_png_bytes: bytes, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(mask_png_bytes)
