"""Tests for `sam3_runtime.py`.

Strategy: never load real SAM 3 weights or torch in CI. The functions
under test are:
  - `analyze_mask(mask, bgr)`: pure numpy / opencv, no model state.
    Tested directly with synthetic masks + frames.
  - `Sam3VideoLabeller.label_video(...)`: full pipeline. Tested by
    monkey-patching the labeller's loaded `_model` / `_processor` with
    stubs that emit a known mask sequence, then asserting the resulting
    SAM3GTRecord has the expected per-frame stats.

This is the same shape as `test_detection_parity.py` — verify the glue
without exercising the heavy model.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import numpy as np
import pytest

import sam3_runtime
from sam3_runtime import Sam3VideoLabeller, analyze_mask
from schemas import SAM3GTRecord


# ----- analyze_mask --------------------------------------------------


def _solid_circle_mask(h: int, w: int, cx: int, cy: int, r: int) -> np.ndarray:
    ys, xs = np.ogrid[:h, :w]
    d2 = (xs - cx) ** 2 + (ys - cy) ** 2
    return (d2 <= r * r).astype(np.uint8) * 255


def _bgr_with_blue_disk(h: int, w: int, cx: int, cy: int, r: int) -> np.ndarray:
    """A solid black BGR frame with a blue-ish disk at (cx, cy)."""
    bgr = np.zeros((h, w, 3), dtype=np.uint8)
    mask = _solid_circle_mask(h, w, cx, cy, r) > 0
    # BGR for "blue ball" ≈ (200, 60, 30) — high B, low G/R.
    bgr[mask] = (200, 60, 30)
    return bgr


def test_analyze_mask_centered_disk():
    h, w = 200, 300
    cx, cy, r = 150, 100, 20
    mask = _solid_circle_mask(h, w, cx, cy, r)
    bgr = _bgr_with_blue_disk(h, w, cx, cy, r)
    stats = analyze_mask(mask, bgr)
    assert stats is not None

    # Bbox sits exactly around the radius — within 1 px tolerance for
    # the discrete grid sampling.
    x_min, y_min, x_max, y_max = stats.bbox
    assert abs(x_min - (cx - r)) <= 1
    assert abs(x_max - (cx + r)) <= 1
    assert abs(y_min - (cy - r)) <= 1
    assert abs(y_max - (cy + r)) <= 1

    # Centroid coincides with disk center.
    assert abs(stats.centroid_px[0] - cx) < 0.5
    assert abs(stats.centroid_px[1] - cy) < 0.5

    # A disk's fill within its bounding square is π/4 ≈ 0.785 in the
    # continuum limit; discretisation pushes it slightly higher.
    assert 0.7 < stats.fill < 0.86

    # A disk has aspect 1.0 by definition.
    assert stats.aspect == pytest.approx(1.0, abs=0.05)

    # Hue for our blue: cv2.cvtColor(BGR=(200,60,30) → HSV) gives H≈110.
    # Use a generous window — exact value depends on opencv internals.
    assert 100 <= stats.hue_mean <= 120
    # Saturation should be high (saturated blue).
    assert stats.sat_mean > 200


def test_analyze_mask_empty_returns_none():
    h, w = 100, 100
    mask = np.zeros((h, w), dtype=np.uint8)
    bgr = np.zeros((h, w, 3), dtype=np.uint8)
    assert analyze_mask(mask, bgr) is None


def test_analyze_mask_accepts_bool_mask():
    h, w = 100, 100
    mask_bool = _solid_circle_mask(h, w, 50, 50, 15) > 0
    bgr = _bgr_with_blue_disk(h, w, 50, 50, 15)
    stats = analyze_mask(mask_bool.astype(np.uint8), bgr)
    assert stats is not None
    assert stats.area_px == int(np.count_nonzero(mask_bool))


# ----- Sam3VideoLabeller (with stubs) --------------------------------


class _FakeModelOutput:
    def __init__(self, frame_idx: int):
        self.frame_idx = frame_idx


class _FakeModel:
    """Yields one frame at a time with a single-object mask + score."""
    def __init__(self, frame_count: int, mask_factory):
        self._frame_count = frame_count
        self._mask_factory = mask_factory

    def propagate_in_video_iterator(self, *, inference_session, max_frame_num_to_track, show_progress_bar=False) -> Iterator:
        for idx in range(self._frame_count):
            inference_session._last_idx = idx
            yield _FakeModelOutput(idx)


class _FakeProcessor:
    """Builds a session that records the per-frame mask the fake model
    would have produced, then `postprocess_outputs` returns it."""
    def __init__(self, frame_count: int, mask_factory, score: float = 0.92):
        self._frame_count = frame_count
        self._mask_factory = mask_factory
        self._score = score

    def init_video_session(self, *, video, **kwargs):
        return SimpleNamespace(_video=video, _last_idx=-1)

    def add_text_prompt(self, *, inference_session, text):
        inference_session._prompt = text
        return inference_session

    def postprocess_outputs(self, session, model_outputs, original_sizes=None):
        idx = model_outputs.frame_idx
        mask = self._mask_factory(idx)
        if mask is None:
            return {"object_ids": _FakeTensor([]), "scores": _FakeTensor([]), "boxes": _FakeTensor([]), "masks": _FakeTensor([])}
        return {
            "object_ids": _FakeTensor([1]),
            "scores": _FakeTensor([self._score]),
            "boxes": _FakeTensor([[0.0, 0.0, 1.0, 1.0]]),
            "masks": [_MaskTensor(mask)],
        }


class _FakeTensor:
    """Just enough of torch.Tensor's surface to satisfy the labeller."""
    def __init__(self, values):
        self._values = np.asarray(values, dtype=np.float32) if len(values) else np.zeros(0, dtype=np.float32)

    def detach(self):
        return self

    def cpu(self):
        return self

    def float(self):
        return self

    def numpy(self):
        return self._values

    def __len__(self):
        return len(self._values)


class _MaskTensor:
    """Wraps a numpy mask so the labeller's `mask.detach().cpu().numpy()`
    call returns the underlying ndarray."""
    def __init__(self, arr: np.ndarray):
        self._arr = arr

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self._arr


def _stub_iter_frames(frames):
    def factory(_path, video_start_pts_s):
        for i, bgr in enumerate(frames):
            yield (video_start_pts_s + i / 240.0, bgr)
    return factory


def _stub_probe_dims(width, height):
    def factory(_path):
        return (width, height)
    return factory


def test_label_video_e2e_with_stub_model(monkeypatch, tmp_path):
    h, w = 200, 300
    n_frames = 5
    cx_start, cy = 150, 100
    radius = 20
    # Fake "ball moves left to right" — disk centroid shifts each frame.
    frames = [
        _bgr_with_blue_disk(h, w, cx_start + 10 * i, cy, radius)
        for i in range(n_frames)
    ]
    masks = [
        _solid_circle_mask(h, w, cx_start + 10 * i, cy, radius)
        for i in range(n_frames)
    ]

    # Drop frame 2's mask to verify the labeller writes 4 frames not 5
    # — i.e. absence is correctly handled.
    masks_with_miss = list(masks)
    masks_with_miss[2] = None

    monkeypatch.setattr(sam3_runtime, "iter_frames", _stub_iter_frames(frames))
    monkeypatch.setattr(sam3_runtime, "probe_dims", _stub_probe_dims(w, h))

    labeller = Sam3VideoLabeller(model_id="fake/sam3", device="cpu")
    # Bypass real load(): inject the stubs directly.
    labeller._model = _FakeModel(n_frames, lambda i: masks_with_miss[i])
    labeller._processor = _FakeProcessor(n_frames, lambda i: masks_with_miss[i])
    labeller._model_version = "fake/sam3 (stub)"

    # `_dtype_for_device` would import torch — bypass it for test by
    # patching at instance level (CPU path returns float32, fake doesn't
    # care about dtype).
    labeller._dtype_for_device = lambda: "float32"  # type: ignore[method-assign]

    record = labeller.label_video(
        mov_path=tmp_path / "fake.mov",
        video_start_pts_s=100.0,
        session_id="s_deadbeef",
        camera_id="A",
        prompt="blue ball",
        min_confidence=0.5,
    )

    assert isinstance(record, SAM3GTRecord)
    assert record.session_id == "s_deadbeef"
    assert record.camera_id == "A"
    assert record.video_dims == (w, h)
    assert record.frames_decoded == n_frames
    assert record.frames_labelled == n_frames - 1  # one miss
    # Frames are sorted by frame_idx and miss-frame-2 is omitted.
    indices = [f.frame_idx for f in record.frames]
    assert indices == [0, 1, 3, 4]
    # PTS were stamped from the stub iterator's video_start_pts_s + i/240.
    assert record.frames[0].t_pts_s == pytest.approx(100.0, abs=0.001)
    assert record.frames[1].t_pts_s == pytest.approx(100.0 + 1 / 240.0, abs=0.001)
    # Centroid moves with the disk.
    assert record.frames[0].centroid_px[0] < record.frames[3].centroid_px[0]
    # Confidence comes from the fake processor.
    assert all(f.confidence == pytest.approx(0.92, abs=0.001) for f in record.frames)
    # Hue / area sanity.
    assert all(100 < f.mask_hue_mean < 120 for f in record.frames)
    assert all(f.mask_area_px > 0 for f in record.frames)

    # Round-trip through JSON to catch schema regressions.
    payload = record.model_dump_json()
    rehydrated = SAM3GTRecord.model_validate_json(payload)
    assert rehydrated == record


def test_label_video_drops_low_confidence(monkeypatch, tmp_path):
    """Detections below `min_confidence` shouldn't appear in the record."""
    h, w = 100, 100
    n_frames = 3
    frames = [_bgr_with_blue_disk(h, w, 50, 50, 12) for _ in range(n_frames)]
    masks = [_solid_circle_mask(h, w, 50, 50, 12) for _ in range(n_frames)]

    monkeypatch.setattr(sam3_runtime, "iter_frames", _stub_iter_frames(frames))
    monkeypatch.setattr(sam3_runtime, "probe_dims", _stub_probe_dims(w, h))

    labeller = Sam3VideoLabeller(device="cpu")
    labeller._model = _FakeModel(n_frames, lambda i: masks[i])
    labeller._processor = _FakeProcessor(n_frames, lambda i: masks[i], score=0.3)
    labeller._model_version = "fake/sam3 (stub)"
    labeller._dtype_for_device = lambda: "float32"  # type: ignore[method-assign]

    record = labeller.label_video(
        mov_path=tmp_path / "fake.mov",
        video_start_pts_s=0.0,
        session_id="s_deadbeef",
        camera_id="A",
        min_confidence=0.5,
    )
    assert record.frames_labelled == 0
    assert record.frames_decoded == n_frames


def test_max_frames_clamps_decode(monkeypatch, tmp_path):
    h, w = 80, 80
    frames = [_bgr_with_blue_disk(h, w, 40, 40, 10) for _ in range(20)]
    masks = [_solid_circle_mask(h, w, 40, 40, 10) for _ in range(20)]

    monkeypatch.setattr(sam3_runtime, "iter_frames", _stub_iter_frames(frames))
    monkeypatch.setattr(sam3_runtime, "probe_dims", _stub_probe_dims(w, h))

    labeller = Sam3VideoLabeller(device="cpu")
    labeller._model = _FakeModel(5, lambda i: masks[i])  # only 5 outputs
    labeller._processor = _FakeProcessor(5, lambda i: masks[i])
    labeller._model_version = "fake/sam3 (stub)"
    labeller._dtype_for_device = lambda: "float32"  # type: ignore[method-assign]

    record = labeller.label_video(
        mov_path=tmp_path / "fake.mov",
        video_start_pts_s=0.0,
        session_id="s_deadbeef",
        camera_id="A",
        max_frames=5,
    )
    assert record.frames_decoded == 5


# ----- time_range / callbacks (mini-plan v4) ------------------------


def _setup_labeller_with_n_frames(monkeypatch, tmp_path, n: int, h: int = 80, w: int = 80):
    frames = [_bgr_with_blue_disk(h, w, 40, 40, 10) for _ in range(n)]
    masks = [_solid_circle_mask(h, w, 40, 40, 10) for _ in range(n)]
    monkeypatch.setattr(sam3_runtime, "iter_frames", _stub_iter_frames(frames))
    monkeypatch.setattr(sam3_runtime, "probe_dims", _stub_probe_dims(w, h))

    labeller = Sam3VideoLabeller(device="cpu")
    # Re-register stubs each time the iterator is consumed; the model
    # iterates over `frame_count` so it must match the post-filter count
    # passed in by individual tests.
    labeller._model_version = "fake/sam3 (stub)"
    labeller._dtype_for_device = lambda: "float32"  # type: ignore[method-assign]
    return labeller, masks


def test_time_range_filters_to_window(monkeypatch, tmp_path):
    """time_range is video-relative seconds; only frames whose
    `absolute_pts_s − video_start_pts_s ∈ [t0, t1]` survive."""
    n_total = 10
    labeller, masks = _setup_labeller_with_n_frames(monkeypatch, tmp_path, n_total)
    # Stub iterator yields PTS = video_start + i/240 for i in 0..9.
    # Picking [2/240, 5/240] should keep frames 2..5 = 4 frames.
    surviving_idx = [2, 3, 4, 5]
    labeller._model = _FakeModel(len(surviving_idx), lambda i: masks[surviving_idx[i]])
    labeller._processor = _FakeProcessor(
        len(surviving_idx), lambda i: masks[surviving_idx[i]]
    )

    record = labeller.label_video(
        mov_path=tmp_path / "fake.mov",
        video_start_pts_s=100.0,
        session_id="s_deadbeef",
        camera_id="A",
        time_range=(2 / 240.0, 5 / 240.0),
    )
    assert record.frames_decoded == 4
    assert record.frames_labelled == 4
    # PTS are still ABSOLUTE (anchor-clock). Tests downstream rely on
    # this contract — validate_three_way matches by t_pts_s.
    assert record.frames[0].t_pts_s == pytest.approx(100.0 + 2 / 240.0, abs=1e-6)
    assert record.frames[-1].t_pts_s == pytest.approx(100.0 + 5 / 240.0, abs=1e-6)


def test_time_range_and_max_frames_conflict_raises(monkeypatch, tmp_path):
    labeller, _ = _setup_labeller_with_n_frames(monkeypatch, tmp_path, 5)
    labeller._model = _FakeModel(0, lambda i: None)
    labeller._processor = _FakeProcessor(0, lambda i: None)
    with pytest.raises(ValueError, match="mutually exclusive"):
        labeller.label_video(
            mov_path=tmp_path / "fake.mov",
            video_start_pts_s=0.0,
            session_id="s_deadbeef",
            camera_id="A",
            max_frames=3,
            time_range=(0.0, 1.0),
        )


def test_time_range_empty_window_raises(monkeypatch, tmp_path):
    labeller, masks = _setup_labeller_with_n_frames(monkeypatch, tmp_path, 5)
    labeller._model = _FakeModel(0, lambda i: None)
    labeller._processor = _FakeProcessor(0, lambda i: None)
    with pytest.raises(RuntimeError, match="no decodable frames"):
        labeller.label_video(
            mov_path=tmp_path / "fake.mov",
            video_start_pts_s=100.0,
            session_id="s_deadbeef",
            camera_id="A",
            # All 5 frames sit at PTS [100.0, 100.017]; window 50..51 is
            # entirely outside, so we expect zero frames after filtering.
            time_range=(50.0, 51.0),
        )


def test_progress_callback_invoked_per_frame(monkeypatch, tmp_path):
    n = 4
    labeller, masks = _setup_labeller_with_n_frames(monkeypatch, tmp_path, n)
    labeller._model = _FakeModel(n, lambda i: masks[i])
    labeller._processor = _FakeProcessor(n, lambda i: masks[i])

    calls: list[tuple[int, int, float]] = []

    def cb(current: int, total: int, mpf: float) -> None:
        calls.append((current, total, mpf))

    labeller.label_video(
        mov_path=tmp_path / "fake.mov",
        video_start_pts_s=0.0,
        session_id="s_deadbeef",
        camera_id="A",
        progress_callback=cb,
    )
    assert len(calls) == n
    # `current` is 1-indexed and increases monotonically; total stays n.
    assert [c[0] for c in calls] == [1, 2, 3, 4]
    assert all(c[1] == n for c in calls)
    # ms_per_frame is non-negative (could be ~0 on synthetic; just a
    # sanity check that we passed something through).
    assert all(c[2] >= 0.0 for c in calls)


def test_preview_callback_invoked_only_on_detection(monkeypatch, tmp_path):
    n = 3
    h, w = 80, 80
    frames = [_bgr_with_blue_disk(h, w, 40, 40, 10) for _ in range(n)]
    masks = [_solid_circle_mask(h, w, 40, 40, 10) for _ in range(n)]
    masks[1] = None  # simulate frame 1 = miss
    monkeypatch.setattr(sam3_runtime, "iter_frames", _stub_iter_frames(frames))
    monkeypatch.setattr(sam3_runtime, "probe_dims", _stub_probe_dims(w, h))

    labeller = Sam3VideoLabeller(device="cpu")
    labeller._model = _FakeModel(n, lambda i: masks[i])
    labeller._processor = _FakeProcessor(n, lambda i: masks[i])
    labeller._model_version = "fake/sam3 (stub)"
    labeller._dtype_for_device = lambda: "float32"  # type: ignore[method-assign]

    seen_frames: list[int] = []

    def preview(frame_idx: int, bgr: np.ndarray, mask: np.ndarray) -> None:
        seen_frames.append(frame_idx)
        # mask should be normalised 0/255 uint8
        assert mask.dtype == np.uint8
        assert set(np.unique(mask).tolist()).issubset({0, 255})
        # bgr is the right shape
        assert bgr.shape == (h, w, 3)

    labeller.label_video(
        mov_path=tmp_path / "fake.mov",
        video_start_pts_s=0.0,
        session_id="s_deadbeef",
        camera_id="A",
        preview_callback=preview,
    )
    # frame 1 was a miss → callback not invoked
    assert seen_frames == [0, 2]


def test_preview_callback_exception_does_not_break_run(monkeypatch, tmp_path):
    """A buggy preview writer mustn't kill the whole labelling job."""
    n = 3
    labeller, masks = _setup_labeller_with_n_frames(monkeypatch, tmp_path, n)
    labeller._model = _FakeModel(n, lambda i: masks[i])
    labeller._processor = _FakeProcessor(n, lambda i: masks[i])

    def angry_preview(*args, **kwargs):
        raise RuntimeError("boom")

    record = labeller.label_video(
        mov_path=tmp_path / "fake.mov",
        video_start_pts_s=0.0,
        session_id="s_deadbeef",
        camera_id="A",
        preview_callback=angry_preview,
    )
    assert record.frames_labelled == n  # still produced GT
