from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from schemas import (
    DetectionPath,
    TrackingExposureCapMode,
    _DEFAULT_PATHS,
    _DEFAULT_TRACKING_EXPOSURE_CAP_MODE,
)

logger = logging.getLogger("ball_tracker")


@dataclass
class SyncParams:
    """Server-owned burst parameters for one mutual-sync run."""

    emit_a_at_s: list[float] = field(default_factory=lambda: [0.3, 0.5, 0.7])
    emit_b_at_s: list[float] = field(default_factory=lambda: [1.8, 2.0, 2.2])
    record_duration_s: float = 4.0
    search_window_s: float = 0.3


class RuntimeSettingsStore:
    """Server-owned tunables persisted across restarts.

    State owns synchronization; this class owns validation, defaults, and the
    JSON shape on disk.
    """

    CHIRP_THRESHOLD_MIN = 0.01
    CHIRP_THRESHOLD_MAX = 1.0
    HEARTBEAT_INTERVAL_MIN = 1.0
    HEARTBEAT_INTERVAL_MAX = 60.0
    ALLOWED_CAPTURE_HEIGHTS = (720, 1080)

    def __init__(
        self,
        path: Path,
        *,
        atomic_write: Callable[[Path, str], None],
    ) -> None:
        self._path = path
        self._atomic_write = atomic_write
        # Live is always on at arm time. server_post is triggered
        # post-hoc per session via /sessions/{sid}/run_server_post —
        # no longer a knob the operator flips at arm time.
        self.default_paths: set[DetectionPath] = set(_DEFAULT_PATHS)
        self.chirp_detect_threshold: float = 0.18
        self.mutual_sync_threshold: float = 0.10
        self.heartbeat_interval_s: float = 1.0
        self.sync_params: SyncParams = SyncParams()
        self.tracking_exposure_cap: TrackingExposureCapMode = _DEFAULT_TRACKING_EXPOSURE_CAP_MODE
        self.capture_height_px: int = 1080
        # MOG2 background subtraction on server_post detection. Default
        # True — it pays for itself on cluttered rigs by removing static
        # yellow-green (buttons, door handles). A runtime kill-switch so
        # operators can A/B a session against pure-HSV when debugging.
        self.detection_bg_subtraction_enabled: bool = True
        self.load()

    def load(self) -> None:
        if not self._path.exists():
            return
        try:
            obj = json.loads(self._path.read_text())
        except Exception as e:
            logger.warning("skip corrupt runtime_settings %s: %s", self._path, e)
            return
        thr = obj.get("chirp_detect_threshold")
        if isinstance(thr, (int, float)) and self.CHIRP_THRESHOLD_MIN <= thr <= self.CHIRP_THRESHOLD_MAX:
            self.chirp_detect_threshold = float(thr)
        mthr = obj.get("mutual_sync_threshold")
        if isinstance(mthr, (int, float)) and self.CHIRP_THRESHOLD_MIN <= mthr <= self.CHIRP_THRESHOLD_MAX:
            self.mutual_sync_threshold = float(mthr)
        ivl = obj.get("heartbeat_interval_s")
        if isinstance(ivl, (int, float)) and self.HEARTBEAT_INTERVAL_MIN <= ivl <= self.HEARTBEAT_INTERVAL_MAX:
            self.heartbeat_interval_s = float(ivl)
        ch = obj.get("capture_height_px")
        if isinstance(ch, int) and ch in self.ALLOWED_CAPTURE_HEIGHTS:
            self.capture_height_px = ch
        tec = obj.get("tracking_exposure_cap")
        if isinstance(tec, str):
            try:
                self.tracking_exposure_cap = TrackingExposureCapMode(tec)
            except ValueError:
                pass
        bg_sub = obj.get("detection_bg_subtraction_enabled")
        if isinstance(bg_sub, bool):
            self.detection_bg_subtraction_enabled = bg_sub
        paths = obj.get("default_paths")
        if isinstance(paths, list):
            parsed: set[DetectionPath] = set()
            for item in paths:
                if not isinstance(item, str):
                    continue
                try:
                    parsed.add(DetectionPath(item))
                except ValueError:
                    continue
            if parsed:
                self.default_paths = parsed
        logger.info(
            "restored runtime_settings: chirp=%.3f interval_s=%.2f capture_h=%d tracking_exposure=%s paths=%s",
            self.chirp_detect_threshold,
            self.heartbeat_interval_s,
            self.capture_height_px,
            self.tracking_exposure_cap.value,
            sorted(p.value for p in self.default_paths),
        )

    def persist(self) -> None:
        payload = json.dumps(
            {
                "chirp_detect_threshold": self.chirp_detect_threshold,
                "mutual_sync_threshold": self.mutual_sync_threshold,
                "heartbeat_interval_s": self.heartbeat_interval_s,
                "capture_height_px": self.capture_height_px,
                "tracking_exposure_cap": self.tracking_exposure_cap.value,
                "default_paths": sorted(p.value for p in self.default_paths),
                "detection_bg_subtraction_enabled": self.detection_bg_subtraction_enabled,
            },
            indent=2,
        )
        self._atomic_write(self._path, payload)

    def set_default_paths(self, paths: set[DetectionPath]) -> set[DetectionPath]:
        if not paths:
            raise ValueError("at least one detection path must be enabled")
        self.default_paths = set(paths)
        self.persist()
        return set(self.default_paths)

    def set_capture_height_px(self, value: int) -> int:
        if not isinstance(value, int):
            raise ValueError("capture_height must be an int")
        if value not in self.ALLOWED_CAPTURE_HEIGHTS:
            raise ValueError(f"capture_height {value} not in {self.ALLOWED_CAPTURE_HEIGHTS}")
        self.capture_height_px = value
        self.persist()
        return value

    def set_chirp_detect_threshold(self, value: float) -> float:
        v = self._validated_threshold(value)
        self.chirp_detect_threshold = v
        self.persist()
        return v

    def set_mutual_sync_threshold(self, value: float) -> float:
        v = self._validated_threshold(value)
        self.mutual_sync_threshold = v
        self.persist()
        return v

    def set_heartbeat_interval_s(self, value: float) -> float:
        if not isinstance(value, (int, float)):
            raise ValueError("interval must be numeric")
        v = float(value)
        if not (self.HEARTBEAT_INTERVAL_MIN <= v <= self.HEARTBEAT_INTERVAL_MAX):
            raise ValueError(
                f"interval {v} out of range "
                f"[{self.HEARTBEAT_INTERVAL_MIN}, {self.HEARTBEAT_INTERVAL_MAX}]"
            )
        self.heartbeat_interval_s = v
        self.persist()
        return v

    def set_detection_bg_subtraction_enabled(self, enabled: bool) -> bool:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be bool")
        self.detection_bg_subtraction_enabled = enabled
        self.persist()
        return enabled

    def set_tracking_exposure_cap(self, mode: TrackingExposureCapMode) -> TrackingExposureCapMode:
        self.tracking_exposure_cap = mode
        self.persist()
        return mode

    def _validated_threshold(self, value: float) -> float:
        if not isinstance(value, (int, float)):
            raise ValueError("threshold must be numeric")
        v = float(value)
        if not (self.CHIRP_THRESHOLD_MIN <= v <= self.CHIRP_THRESHOLD_MAX):
            raise ValueError(
                f"threshold {v} out of range "
                f"[{self.CHIRP_THRESHOLD_MIN}, {self.CHIRP_THRESHOLD_MAX}]"
            )
        return v
