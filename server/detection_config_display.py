from __future__ import annotations

from typing import Any

from presets import Preset
from schemas import DetectionConfigSnapshotPayload, PitchPayload, SessionResult


def _legacy_snapshot(
    pitch: PitchPayload,
    *,
    source: str,
) -> DetectionConfigSnapshotPayload | None:
    if pitch.hsv_range_used is None or pitch.shape_gate_used is None:
        return None
    return DetectionConfigSnapshotPayload(
        source=source,
        preset=None,
        hsv_range=pitch.hsv_range_used,
        shape_gate=pitch.shape_gate_used,
    )


def _pitch_live_snapshot(pitch: PitchPayload) -> DetectionConfigSnapshotPayload | None:
    if pitch.live_config_used is not None:
        return pitch.live_config_used
    if pitch.server_post_ran_at is None:
        return _legacy_snapshot(pitch, source="legacy-live")
    return None


def _pitch_server_snapshot(pitch: PitchPayload) -> DetectionConfigSnapshotPayload | None:
    if pitch.server_post_config_used is not None:
        return pitch.server_post_config_used
    if pitch.server_post_ran_at is not None:
        return _legacy_snapshot(pitch, source="legacy-server_post")
    return None


def _pick_a_then_b(
    a: DetectionConfigSnapshotPayload | None,
    b: DetectionConfigSnapshotPayload | None,
) -> DetectionConfigSnapshotPayload | None:
    return a if a is not None else b


def config_snapshots_for_session(
    result: SessionResult | None,
    pitches: dict[str, PitchPayload],
) -> dict[str, DetectionConfigSnapshotPayload | None]:
    if result is not None:
        live = result.live_config_used
        server_post = result.server_post_config_used
    else:
        live = None
        server_post = None
    a = pitches.get("A")
    b = pitches.get("B")
    return {
        "live": live if live is not None else _pick_a_then_b(
            _pitch_live_snapshot(a) if a is not None else None,
            _pitch_live_snapshot(b) if b is not None else None,
        ),
        "server_post": server_post if server_post is not None else _pick_a_then_b(
            _pitch_server_snapshot(a) if a is not None else None,
            _pitch_server_snapshot(b) if b is not None else None,
        ),
    }


def _match_preset(
    snapshot: DetectionConfigSnapshotPayload,
    presets: list[Preset],
) -> str | None:
    """Resolve which on-disk preset (if any) the snapshot's HSV+shape_gate
    pair corresponds to. Disk is the authority for preset identity — a
    snapshot stamped with `preset='tennis'` whose values were later edited
    away from the on-disk `tennis.json` is no longer that preset."""
    hsv = snapshot.hsv_range
    gate = snapshot.shape_gate
    for p in presets:
        if (
            p.hsv.h_min == hsv.h_min and p.hsv.h_max == hsv.h_max
            and p.hsv.s_min == hsv.s_min and p.hsv.s_max == hsv.s_max
            and p.hsv.v_min == hsv.v_min and p.hsv.v_max == hsv.v_max
            and p.shape_gate.aspect_min == gate.aspect_min
            and p.shape_gate.fill_min == gate.fill_min
        ):
            return p.name
    return None


def snapshot_summary(
    snapshot: DetectionConfigSnapshotPayload | None,
    presets: list[Preset] | None = None,
) -> dict[str, Any]:
    if snapshot is None:
        return {
            "available": False,
            "tag": "n/a",
            "detail": "unavailable",
            "title": "Detection config unavailable for this source.",
        }
    hsv = snapshot.hsv_range
    gate = snapshot.shape_gate
    matched = _match_preset(snapshot, presets or [])
    tag = matched or "custom"
    detail = (
        f"h{hsv.h_min}-{hsv.h_max} s{hsv.s_min}-{hsv.s_max} "
        f"v{hsv.v_min}-{hsv.v_max} a>={gate.aspect_min:.2f} f>={gate.fill_min:.2f}"
    )
    title = f"{tag} · {detail}"
    return {
        "available": True,
        "tag": tag,
        "detail": detail,
        "title": title,
    }
