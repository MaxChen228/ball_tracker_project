"""Reference BallDetector V11 — pre-DL, stateless, emit-all-blobs.

Designed to be 1:1 portable to iOS Swift / OpenCV-iOS. Every step here
maps to one OpenCV call available on iOS:

  RGB→HSV         : cv2.cvtColor                 (Swift: cv::cvtColor)
  inRange         : cv2.inRange                  (Swift: cv::inRange)
  morphClose      : cv2.morphologyEx             (Swift: cv::morphologyEx)
  CC w/ stats     : cv2.connectedComponentsWithStats
                                                 (Swift: cv::connectedComponentsWithStats)

No motion gate (rejected, -3.5pp on mid-HSV). No top-K cap (production
iOS BallDetector.mm emits all blobs; server pairing.py physics-gates).

Calibrated from 1073 SAM2 GT frames across 9 sessions. See
lab-research/notes/02_v11_followup.md for V11 derivation
(supersedes 01_final_report.md V10).
"""
from __future__ import annotations
from dataclasses import dataclass
from collections import deque
import numpy as np
import cv2


@dataclass
class BallDetectorConfig:
    """V11 — calibrated against 1073 SAM2 GT frames across 9 sessions
    (V10 baseline 0.884 → V11 0.905, +2.14pp, 0 session regression).
    Strictly Pareto-dominant over V10 and production. See
    lab-research/notes/02_v11_followup.md."""
    # HSV cube — MID width. Wider than production (H[105,112]) to recover
    # shadowed-ball frames; narrower than full-wide (H[100,125]) to avoid
    # merging ball with adjacent blue clutter (regression observed on
    # 22d1835e_b: -19pp at full-wide).
    h_min: int = 103; h_max: int = 118
    s_min: int = 120; s_max: int = 255
    v_min: int = 30;  v_max: int = 255
    # NO motion gate. Adds nothing at mid HSV width (static clutter not
    # over-included) and kills slow-ball frames (-3.5pp macro, -4 frames
    # on 22d1835e_b empirically).
    use_motion_gate: bool = False
    # Morphological CLOSE 3x3 — connects HSV mask fragmentation. Marginal
    # alone (+0.19pp) but stacks cleanly with looser aspect (E5 result).
    close_kernel_px: int = 3
    # Aspect 0.40 (V10 was 0.50). Physical motivation: 240fps motion blur
    # upper bound aspect ~0.62 (12.5cm displacement / 7.8cm diameter at
    # 30 m/s). 0.40 leaves margin without admitting elongated clutter.
    aspect_min: float = 0.40
    fill_min: float = 0.35
    # min_area 3 (V10 was 5). Recovers tiny CCs in fragmented edge frames
    # at the cost of ~5 extra cands/frame on 9-session corpus.
    min_area_px: int = 3
    max_area_px: int = 150_000
    # No top-K cap — emit every blob passing gates (mirrors production
    # iOS BallDetector.mm:detectAllCandidatesScratch behaviour). Server
    # already iterates frame_a.candidates × frame_b.candidates pairwise
    # and physics-gates by gap_threshold_m.


@dataclass
class Candidate:
    px: float
    py: float
    area: int
    aspect: float
    fill: float
    score: float


class BallDetector:
    """Stateful frame-by-frame detector. Call .detect(bgr_frame) per frame.
    Maintains a small rolling buffer of grayscale frames for motion gate.

    Returns list[Candidate] sorted by score desc, length <= top_k.
    First (lag) frames return empty list (motion gate has no prior).
    """

    def __init__(self, cfg: BallDetectorConfig | None = None):
        self.cfg = cfg or BallDetectorConfig()
        self._lo = np.array([self.cfg.h_min, self.cfg.s_min, self.cfg.v_min], dtype=np.uint8)
        self._hi = np.array([self.cfg.h_max, self.cfg.s_max, self.cfg.v_max], dtype=np.uint8)

    def reset(self):
        pass  # stateless

    def detect(self, bgr: np.ndarray) -> list[Candidate]:
        cfg = self.cfg
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, self._lo, self._hi)
        if cfg.close_kernel_px > 0:
            k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                          (cfg.close_kernel_px, cfg.close_kernel_px))
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
        n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
        out: list[Candidate] = []
        for i in range(1, n):
            a = int(stats[i, cv2.CC_STAT_AREA])
            if a < cfg.min_area_px or a > cfg.max_area_px:
                continue
            w = int(stats[i, cv2.CC_STAT_WIDTH]); h = int(stats[i, cv2.CC_STAT_HEIGHT])
            if w <= 0 or h <= 0:
                continue
            asp = min(w, h) / max(w, h)
            if asp < cfg.aspect_min:
                continue
            fill = a / (w * h)
            if fill < cfg.fill_min:
                continue
            out.append(Candidate(
                px=float(cents[i, 0]), py=float(cents[i, 1]),
                area=a, aspect=asp, fill=fill, score=float(a),  # area-desc, mirroring iOS
            ))
        out.sort(key=lambda c: c.area, reverse=True)
        return out


# ---------- benchmark / smoke test ----------
def _bench(n_frames: int = 240):
    """Bench on real session frames (representative HSV mask density)."""
    import time, json
    from pathlib import Path
    WS = Path(__file__).resolve().parents[2] / "lab" / "standalone_workspace"
    m = json.loads((WS / "manifest.json").read_text())
    item = next(it for it in m["items"] if it.get("propagate_status") == "done")
    fps = sorted((WS / "items" / item["slug"] / "frames").glob("*.jpg"))[:n_frames]
    frames = [cv2.imread(str(p), cv2.IMREAD_COLOR) for p in fps]
    frames = [f for f in frames if f is not None]
    cfg = BallDetectorConfig()
    det = BallDetector(cfg)
    for f in frames[:5]: det.detect(f)
    det.reset()
    t0 = time.perf_counter()
    total_cands = 0
    for f in frames: total_cands += len(det.detect(f))
    t1 = time.perf_counter()
    per_frame_ms = (t1 - t0) / len(frames) * 1000
    print(f"[bench] {len(frames)} real session frames @ 1920x1080  =>  "
          f"{per_frame_ms:.2f} ms/frame  ({1000/per_frame_ms:.0f} fps single-threaded)  "
          f"avg {total_cands/len(frames):.1f} cands/frame")
    return per_frame_ms


if __name__ == "__main__":
    import sys
    if "--bench" in sys.argv:
        _bench()
    else:
        # Smoke test on real GT data
        from pathlib import Path
        import json
        WS = Path(__file__).resolve().parents[2] / "lab" / "standalone_workspace"
        m = json.loads((WS/"manifest.json").read_text())
        item = next(it for it in m["items"] if it.get("propagate_status")=="done")
        slug = item["slug"]; in_f = item["in_frame"]
        det = BallDetector()
        for fp in sorted((WS/"items"/slug/"frames").glob("*.jpg"))[:8]:
            f = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            cands = det.detect(f)
            top = f"{cands[0].score:.3f}" if cands else "n/a"
            print(f"{fp.name}: {len(cands)} cands, top score = {top}")
