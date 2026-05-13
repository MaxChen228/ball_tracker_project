from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from schemas import (
    DetectionPath,
    TrackingExposureCapMode,
    _DEFAULT_PATHS,
    _DEFAULT_TRACKING_EXPOSURE_CAP_MODE,
)
from strike_zone import (
    DEFAULT_BATTER_HEIGHT_CM,
    validate_batter_height_cm,
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
    SYNC_RECORD_DURATION_MIN = 1.0
    SYNC_RECORD_DURATION_MAX = 30.0
    SYNC_SEARCH_WINDOW_MIN = 0.05
    SYNC_SEARCH_WINDOW_MAX = 2.0

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
        self.batter_height_cm: int = DEFAULT_BATTER_HEIGHT_CM
        self.load()

    def load(self) -> None:
        """Strict loader: any malformed or out-of-range field raises and
        kills boot. Silent-fallback to ctor defaults would let an
        operator's hand-edited `runtime_settings.json` partially apply
        (good fields kept, bad fields reverted) which is the exact
        research-contaminating behaviour CLAUDE.md forbids — they'd
        think they're testing chirp=0.18 but actually be at the seed
        default 0.10 because the value was typo'd to a string.

        Crashing boot is the correct response: operator sees the error
        immediately, fixes the file (or deletes it to start fresh),
        restarts."""
        if not self._path.exists():
            return
        path = self._path
        try:
            obj = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise ValueError(f"{path} is not valid JSON: {e}") from e
        if not isinstance(obj, dict):
            raise ValueError(f"{path} must be a JSON object")

        if "chirp_detect_threshold" not in obj:
            raise ValueError(f"{path} missing required 'chirp_detect_threshold'")
        thr = obj["chirp_detect_threshold"]
        if not isinstance(thr, (int, float)):
            raise ValueError(
                f"{path} 'chirp_detect_threshold' must be numeric, got {type(thr).__name__}"
            )
        if not (self.CHIRP_THRESHOLD_MIN <= thr <= self.CHIRP_THRESHOLD_MAX):
            raise ValueError(
                f"{path} 'chirp_detect_threshold' {thr} out of range "
                f"[{self.CHIRP_THRESHOLD_MIN}, {self.CHIRP_THRESHOLD_MAX}]"
            )
        self.chirp_detect_threshold = float(thr)

        if "mutual_sync_threshold" not in obj:
            raise ValueError(f"{path} missing required 'mutual_sync_threshold'")
        mthr = obj["mutual_sync_threshold"]
        if not isinstance(mthr, (int, float)):
            raise ValueError(
                f"{path} 'mutual_sync_threshold' must be numeric, got {type(mthr).__name__}"
            )
        if not (self.CHIRP_THRESHOLD_MIN <= mthr <= self.CHIRP_THRESHOLD_MAX):
            raise ValueError(
                f"{path} 'mutual_sync_threshold' {mthr} out of range "
                f"[{self.CHIRP_THRESHOLD_MIN}, {self.CHIRP_THRESHOLD_MAX}]"
            )
        self.mutual_sync_threshold = float(mthr)

        if "heartbeat_interval_s" not in obj:
            raise ValueError(f"{path} missing required 'heartbeat_interval_s'")
        ivl = obj["heartbeat_interval_s"]
        if not isinstance(ivl, (int, float)):
            raise ValueError(
                f"{path} 'heartbeat_interval_s' must be numeric, got {type(ivl).__name__}"
            )
        if not (self.HEARTBEAT_INTERVAL_MIN <= ivl <= self.HEARTBEAT_INTERVAL_MAX):
            raise ValueError(
                f"{path} 'heartbeat_interval_s' {ivl} out of range "
                f"[{self.HEARTBEAT_INTERVAL_MIN}, {self.HEARTBEAT_INTERVAL_MAX}]"
            )
        self.heartbeat_interval_s = float(ivl)

        if "sync_params" not in obj:
            raise ValueError(f"{path} missing required 'sync_params'")
        # _validated_sync_params raises ValueError on any inner field; let it
        # propagate so the operator sees the exact field that failed.
        self.sync_params = self._validated_sync_params(obj["sync_params"])

        if "capture_height_px" not in obj:
            raise ValueError(f"{path} missing required 'capture_height_px'")
        ch = obj["capture_height_px"]
        if not isinstance(ch, int) or isinstance(ch, bool):
            raise ValueError(
                f"{path} 'capture_height_px' must be int, got {type(ch).__name__}"
            )
        if ch not in self.ALLOWED_CAPTURE_HEIGHTS:
            raise ValueError(
                f"{path} 'capture_height_px' {ch} not in {self.ALLOWED_CAPTURE_HEIGHTS}"
            )
        self.capture_height_px = ch

        if "tracking_exposure_cap" not in obj:
            raise ValueError(f"{path} missing required 'tracking_exposure_cap'")
        tec = obj["tracking_exposure_cap"]
        if not isinstance(tec, str):
            raise ValueError(
                f"{path} 'tracking_exposure_cap' must be a string, got {type(tec).__name__}"
            )
        try:
            self.tracking_exposure_cap = TrackingExposureCapMode(tec)
        except ValueError as e:
            raise ValueError(
                f"{path} 'tracking_exposure_cap' {tec!r} is not a valid mode: {e}"
            ) from e

        if "strike_zone" not in obj:
            raise ValueError(f"{path} missing required 'strike_zone'")
        sz = obj["strike_zone"]
        if not isinstance(sz, dict):
            raise ValueError(
                f"{path} 'strike_zone' must be an object, got {type(sz).__name__}"
            )
        if "batter_height_cm" not in sz:
            raise ValueError(f"{path} 'strike_zone' missing required 'batter_height_cm'")
        raw_height = sz["batter_height_cm"]
        if not isinstance(raw_height, int) or isinstance(raw_height, bool):
            raise ValueError(
                f"{path} 'strike_zone.batter_height_cm' must be int, "
                f"got {type(raw_height).__name__}"
            )
        # validate_batter_height_cm raises ValueError on out-of-range; let it propagate.
        self.batter_height_cm = validate_batter_height_cm(raw_height)

        if "default_paths" not in obj:
            raise ValueError(f"{path} missing required 'default_paths'")
        paths = obj["default_paths"]
        if not isinstance(paths, list):
            raise ValueError(
                f"{path} 'default_paths' must be an array, got {type(paths).__name__}"
            )
        if not paths:
            raise ValueError(f"{path} 'default_paths' must be non-empty")
        parsed: set[DetectionPath] = set()
        for item in paths:
            if not isinstance(item, str):
                raise ValueError(
                    f"{path} 'default_paths' entries must be strings, "
                    f"got {type(item).__name__}"
                )
            try:
                parsed.add(DetectionPath(item))
            except ValueError as e:
                raise ValueError(
                    f"{path} 'default_paths' entry {item!r} is not a valid path: {e}"
                ) from e
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
                "sync_params": {
                    "emit_a_at_s": self.sync_params.emit_a_at_s,
                    "emit_b_at_s": self.sync_params.emit_b_at_s,
                    "record_duration_s": self.sync_params.record_duration_s,
                    "search_window_s": self.sync_params.search_window_s,
                },
                "capture_height_px": self.capture_height_px,
                "tracking_exposure_cap": self.tracking_exposure_cap.value,
                "strike_zone": {
                    "batter_height_cm": self.batter_height_cm,
                },
                "default_paths": sorted(p.value for p in self.default_paths),
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

    def set_sync_params(self, params: SyncParams) -> SyncParams:
        v = self._validated_sync_params(
            {
                "emit_a_at_s": params.emit_a_at_s,
                "emit_b_at_s": params.emit_b_at_s,
                "record_duration_s": params.record_duration_s,
                "search_window_s": params.search_window_s,
            }
        )
        self.sync_params = v
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

    def set_tracking_exposure_cap(self, mode: TrackingExposureCapMode) -> TrackingExposureCapMode:
        self.tracking_exposure_cap = mode
        self.persist()
        return mode

    def set_batter_height_cm(self, value: int) -> int:
        self.batter_height_cm = validate_batter_height_cm(value)
        self.persist()
        return self.batter_height_cm

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

    def _validated_sync_params(self, obj: object) -> SyncParams:
        if not isinstance(obj, dict):
            raise ValueError("sync_params must be an object")
        dur = self._validated_float(
            obj.get("record_duration_s"),
            "record_duration_s",
            self.SYNC_RECORD_DURATION_MIN,
            self.SYNC_RECORD_DURATION_MAX,
        )
        win = self._validated_float(
            obj.get("search_window_s"),
            "search_window_s",
            self.SYNC_SEARCH_WINDOW_MIN,
            self.SYNC_SEARCH_WINDOW_MAX,
        )
        return SyncParams(
            emit_a_at_s=self._validated_emit_times(obj.get("emit_a_at_s"), "emit_a_at_s", dur),
            emit_b_at_s=self._validated_emit_times(obj.get("emit_b_at_s"), "emit_b_at_s", dur),
            record_duration_s=dur,
            search_window_s=win,
        )

    def _validated_emit_times(self, value: object, name: str, duration_s: float) -> list[float]:
        if not isinstance(value, list):
            raise ValueError(f"{name} must be an array")
        if not value:
            raise ValueError(f"{name} must be non-empty")
        out: list[float] = []
        for raw in value:
            if not isinstance(raw, (int, float)):
                raise ValueError(f"{name} entries must be numeric")
            t = float(raw)
            if not math.isfinite(t):
                raise ValueError(f"{name} entries must be finite")
            if t < 0.0 or t > duration_s:
                raise ValueError(f"{name} entry {t} outside recording duration {duration_s}")
            out.append(t)
        return out

    def _validated_float(self, value: object, name: str, min_value: float, max_value: float) -> float:
        if not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be numeric")
        v = float(value)
        if not math.isfinite(v):
            raise ValueError(f"{name} must be finite")
        if not (min_value <= v <= max_value):
            raise ValueError(f"{name} {v} out of range [{min_value}, {max_value}]")
        return v
