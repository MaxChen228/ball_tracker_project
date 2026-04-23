from __future__ import annotations

from typing import Callable

from schemas import Device


class DeviceRegistry:
    """Heartbeat-backed camera liveness registry."""

    def __init__(
        self,
        *,
        time_fn: Callable[[], float],
        stale_after_s: float,
        gc_after_s: float,
        cap: int,
    ) -> None:
        self._time_fn = time_fn
        self._stale_after_s = stale_after_s
        self._gc_after_s = gc_after_s
        self._cap = cap
        self.devices: dict[str, Device] = {}

    def heartbeat(
        self,
        camera_id: str,
        *,
        time_synced: bool = False,
        time_sync_id: str | None = None,
        sync_anchor_timestamp_s: float | None = None,
        battery_level: float | None = None,
        battery_state: str | None = None,
    ) -> None:
        now = self._time_fn()
        # Preserve last-known battery if this particular heartbeat omits it
        # (keeps the UI stable across stray packets) but update when present.
        prev = self.devices.get(camera_id)
        resolved_level = battery_level if battery_level is not None else (prev.battery_level if prev else None)
        resolved_state = battery_state if battery_state is not None else (prev.battery_state if prev else None)
        self.devices[camera_id] = Device(
            camera_id=camera_id,
            last_seen_at=now,
            time_synced=time_synced,
            time_sync_id=(time_sync_id if time_synced else None),
            time_sync_at=(now if time_synced and time_sync_id is not None else None),
            sync_anchor_timestamp_s=(
                float(sync_anchor_timestamp_s)
                if time_synced and sync_anchor_timestamp_s is not None
                else None
            ),
            battery_level=resolved_level,
            battery_state=resolved_state,
        )
        stale = [
            cam for cam, dev in self.devices.items()
            if now - dev.last_seen_at > self._gc_after_s
        ]
        for cam in stale:
            del self.devices[cam]
        while len(self.devices) > self._cap:
            oldest = min(
                self.devices.items(),
                key=lambda kv: kv[1].last_seen_at,
            )[0]
            del self.devices[oldest]

    def mark_offline(self, camera_id: str) -> None:
        dev = self.devices.get(camera_id)
        if dev is None:
            return
        self.devices[camera_id] = Device(
            camera_id=dev.camera_id,
            last_seen_at=self._time_fn() - self._stale_after_s - 0.1,
            time_synced=dev.time_synced,
            time_sync_id=dev.time_sync_id,
            time_sync_at=dev.time_sync_at,
            sync_anchor_timestamp_s=dev.sync_anchor_timestamp_s,
            battery_level=dev.battery_level,
            battery_state=dev.battery_state,
        )

    def online(self, stale_after_s: float | None = None) -> list[Device]:
        now = self._time_fn()
        threshold = self._stale_after_s if stale_after_s is None else stale_after_s
        fresh = [
            d for d in self.devices.values()
            if now - d.last_seen_at <= threshold
        ]
        fresh.sort(key=lambda d: d.camera_id)
        return fresh

    def known_camera_ids(self) -> list[str]:
        return list(self.devices.keys())

    def snapshot(self, camera_id: str) -> Device | None:
        dev = self.devices.get(camera_id)
        if dev is None:
            return None
        return Device(
            camera_id=dev.camera_id,
            last_seen_at=dev.last_seen_at,
            time_synced=dev.time_synced,
            time_sync_id=dev.time_sync_id,
            time_sync_at=dev.time_sync_at,
            sync_anchor_timestamp_s=dev.sync_anchor_timestamp_s,
            battery_level=dev.battery_level,
            battery_state=dev.battery_state,
        )

    def values(self) -> list[Device]:
        return list(self.devices.values())

    def get(self, camera_id: str) -> Device | None:
        return self.devices.get(camera_id)

    def clear(self) -> None:
        self.devices.clear()
