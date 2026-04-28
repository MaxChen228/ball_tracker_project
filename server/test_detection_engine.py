"""Tests for `DetectionEngine` Protocol + `HSVDetectionEngine` impl.

Exercises three contracts that the rest of the codebase relies on:
  1. `HSVDetectionEngine.detect` is byte-for-byte equivalent to the
     legacy `detect_ball_with_candidates` free-function call when the
     same configuration is supplied. Any divergence would silently
     change pitch JSONs the moment a session re-runs server_post.
  2. `detect_pitch` stamps `engine.name` on every produced FramePayload
     (detection AND no-detection branches).
  3. `LivePairingSession._resolve_candidates` stamps the iOS engine
     identity on resolved frames so `frames_live` records who detected.

The schema-level field is non-required (legacy pitch JSONs lack it),
but every production write path MUST populate it — these tests are
the gate that enforces the production-write requirement.
"""
from __future__ import annotations

import numpy as np
import pytest

from candidate_selector import CandidateSelectorTuning
from detection import HSVRange, ShapeGate, detect_ball_with_candidates
from detection_engine import (
    HSV_IOS_ENGINE_NAME,
    HSV_SERVER_ENGINE_NAME,
    DetectionEngine,
    HSVDetectionEngine,
)
from live_pairing import LivePairingSession
from pipeline import detect_pitch
from schemas import BlobCandidate, FramePayload


def _frame_with_blue_circle() -> np.ndarray:
    """1080p BGR canvas with a single solid blue blob in the centre.
    HSVRange.default() targets yellow-green so we use the blue_ball
    preset HSV via a custom range."""
    img = np.zeros((1080, 1920, 3), dtype=np.uint8)
    cv2_circle = __import__("cv2").circle
    cv2_circle(img, (960, 540), 30, (255, 50, 0), -1)  # BGR blue
    return img


def _blue_ball_hsv() -> HSVRange:
    return HSVRange(h_min=100, h_max=130, s_min=140, s_max=255, v_min=40, v_max=255)


# ---- HSVDetectionEngine identity ---------------------------------------

def test_server_engine_name_is_versioned():
    engine = HSVDetectionEngine(HSVRange.default())
    assert engine.name == HSV_SERVER_ENGINE_NAME == "hsv@1.0"


def test_ios_engine_identity_is_distinct_from_server():
    """server_post and live paths are byte-aligned algorithm but run on
    different inputs (BGRA vs H.264-decoded BGR). Identifiers MUST stay
    distinct so archived data preserves which side actually ran."""
    assert HSV_IOS_ENGINE_NAME == "hsv@ios.1.0"
    assert HSV_IOS_ENGINE_NAME != HSV_SERVER_ENGINE_NAME


def test_engine_satisfies_protocol():
    """Static-ish protocol conformance: HSVDetectionEngine should be
    accepted anywhere `DetectionEngine` is expected."""
    engine: DetectionEngine = HSVDetectionEngine(HSVRange.default())
    assert hasattr(engine, "name")
    assert callable(engine.detect)


# ---- Parity with the free function -------------------------------------

def test_engine_detect_matches_free_function():
    """HSVDetectionEngine MUST produce identical output to the legacy
    detect_ball_with_candidates with matching config — otherwise the
    refactor silently changes detection results across the codebase."""
    img = _frame_with_blue_circle()
    hsv = _blue_ball_hsv()
    gate = ShapeGate(aspect_min=0.7, fill_min=0.55)
    tuning = CandidateSelectorTuning.default()

    engine = HSVDetectionEngine(hsv_range=hsv, shape_gate=gate, selector_tuning=tuning)

    via_engine = engine.detect(
        img,
        prev_position=(960.0, 540.0),
        prev_velocity=(0.0, 0.0),
        dt=1 / 240,
    )
    via_func = detect_ball_with_candidates(
        img, hsv,
        prev_position=(960.0, 540.0),
        prev_velocity=(0.0, 0.0),
        dt=1 / 240,
        shape_gate=gate,
        selector_tuning=tuning,
    )
    # Pydantic models compare structurally.
    assert via_engine[0] == via_func[0]
    assert via_engine[1] == via_func[1]


def test_engine_detect_propagates_no_match():
    """Empty / black frame → both winner and blobs empty, regardless of
    engine wrapper."""
    img = np.zeros((1080, 1920, 3), dtype=np.uint8)
    engine = HSVDetectionEngine(HSVRange.default())
    winner, blobs = engine.detect(img)
    assert winner is None
    assert blobs == []


# ---- detect_pitch stamps engine identity -------------------------------

def test_detect_pitch_stamps_engine_name_on_every_frame():
    """Both ball-detected and no-detection branches MUST set
    detection_engine — historical pitches stay queryable by engine."""
    img = _frame_with_blue_circle()
    blank = np.zeros((1080, 1920, 3), dtype=np.uint8)

    def fake_iter(_path, _start):
        # Mix one positive frame and one empty frame so both code
        # branches in detect_pitch run.
        yield (0.0, img)
        yield (1 / 240, blank)

    engine = HSVDetectionEngine(_blue_ball_hsv())
    out = detect_pitch(
        video_path=__import__("pathlib").Path("/tmp/fake.mov"),
        video_start_pts_s=0.0,
        frame_iter=fake_iter,
        engine=engine,
    )
    assert len(out) == 2
    for frame in out:
        assert frame.detection_engine == "hsv@1.0", (
            f"missing/wrong engine on frame: {frame!r}"
        )


def test_detect_pitch_default_engine_uses_hsv_with_supplied_range():
    """No engine + hsv_range supplied → builds default HSVDetectionEngine
    that uses the supplied range. The stamped name is `hsv@1.0`."""
    img = _frame_with_blue_circle()

    def fake_iter(_path, _start):
        yield (0.0, img)

    out = detect_pitch(
        video_path=__import__("pathlib").Path("/tmp/fake.mov"),
        video_start_pts_s=0.0,
        frame_iter=fake_iter,
        hsv_range=_blue_ball_hsv(),
    )
    assert len(out) == 1
    assert out[0].detection_engine == "hsv@1.0"
    assert out[0].ball_detected is True


# ---- live pairing stamps iOS engine identity ---------------------------

def test_live_resolve_stamps_ios_engine_when_input_has_none():
    sess = LivePairingSession(session_id="s_test1234")
    cand = BlobCandidate(px=100.0, py=200.0, area=300, area_score=1.0)
    frame = FramePayload(
        frame_index=0, timestamp_s=0.0,
        candidates=[cand], ball_detected=False,  # detection_engine left None
    )
    resolved = sess._resolve_candidates("A", frame)
    assert resolved.detection_engine == HSV_IOS_ENGINE_NAME
    assert resolved.ball_detected is True
    assert resolved.px == 100.0


def test_live_resolve_preserves_engine_when_input_has_one():
    """If iOS Phase 2 starts sending the engine field on the wire, the
    server MUST NOT clobber it with the default — that would erase
    information about which engine actually produced the detection."""
    sess = LivePairingSession(session_id="s_test1234")
    cand = BlobCandidate(px=100.0, py=200.0, area=300, area_score=1.0)
    frame = FramePayload(
        frame_index=0, timestamp_s=0.0,
        candidates=[cand], ball_detected=False,
        detection_engine="ml@ios.deadbeef",
    )
    resolved = sess._resolve_candidates("A", frame)
    assert resolved.detection_engine == "ml@ios.deadbeef"


def test_live_resolve_stamps_engine_on_empty_candidates():
    """Empty candidate list still sets the engine — None vs empty-list
    is a real distinction (detector ran but found nothing)."""
    sess = LivePairingSession(session_id="s_test1234")
    frame = FramePayload(
        frame_index=0, timestamp_s=0.0,
        candidates=[], ball_detected=False,
    )
    resolved = sess._resolve_candidates("A", frame)
    assert resolved.detection_engine == HSV_IOS_ENGINE_NAME
    assert resolved.ball_detected is False


# ---- schema field non-required for legacy reads ------------------------

def test_frame_payload_loads_legacy_json_without_engine_field():
    """Existing on-disk pitch JSONs lack detection_engine. Loader MUST
    accept them — otherwise a server restart hard-fails on historical
    data. Constructing FramePayload with no detection_engine kwarg
    should leave it as None, not raise."""
    f = FramePayload(frame_index=0, timestamp_s=0.0, ball_detected=False)
    assert f.detection_engine is None
