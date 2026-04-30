from __future__ import annotations

from dataclasses import dataclass


DEFAULT_BATTER_HEIGHT_CM = 175
MIN_BATTER_HEIGHT_CM = 120
MAX_BATTER_HEIGHT_CM = 220

BASELINE_BATTER_HEIGHT_CM = 175.0
BASELINE_Z_BOTTOM_M = 0.46
BASELINE_Z_TOP_M = 1.06

PLATE_WIDTH_M = 0.432
PLATE_SHOULDER_Y_M = 0.216
PLATE_TIP_Y_M = 0.432

STRIKE_ZONE_X_HALF_M = PLATE_WIDTH_M / 2
STRIKE_ZONE_Y_FRONT_M = 0.0
STRIKE_ZONE_Y_BACK_M = PLATE_TIP_Y_M


@dataclass(frozen=True)
class StrikeZoneGeometry:
    batter_height_cm: int
    x_half_m: float
    y_front_m: float
    y_back_m: float
    z_bottom_m: float
    z_top_m: float
    z_height_m: float
    front_face: list[list[float]]
    back_face: list[list[float]]
    connectors: list[list[list[float]]]
    front_grid: list[list[list[float]]]

    def to_dict(self) -> dict:
        return {
            "batter_height_cm": self.batter_height_cm,
            "x_half_m": self.x_half_m,
            "y_front_m": self.y_front_m,
            "y_back_m": self.y_back_m,
            "z_bottom_m": self.z_bottom_m,
            "z_top_m": self.z_top_m,
            "z_height_m": self.z_height_m,
            "front_face": self.front_face,
            "back_face": self.back_face,
            "connectors": self.connectors,
            "front_grid": self.front_grid,
        }


def validate_batter_height_cm(value: int) -> int:
    if not isinstance(value, int):
        raise ValueError("batter_height_cm must be an int")
    if not (MIN_BATTER_HEIGHT_CM <= value <= MAX_BATTER_HEIGHT_CM):
        raise ValueError(
            f"batter_height_cm {value} out of range "
            f"[{MIN_BATTER_HEIGHT_CM}, {MAX_BATTER_HEIGHT_CM}]"
        )
    return value


def strike_zone_geometry_for_height(height_cm: int) -> StrikeZoneGeometry:
    height_cm = validate_batter_height_cm(height_cm)
    scale = float(height_cm) / BASELINE_BATTER_HEIGHT_CM
    z_bottom_m = BASELINE_Z_BOTTOM_M * scale
    z_top_m = BASELINE_Z_TOP_M * scale
    z_height_m = z_top_m - z_bottom_m

    x_left = -STRIKE_ZONE_X_HALF_M
    x_right = STRIKE_ZONE_X_HALF_M
    y_front = STRIKE_ZONE_Y_FRONT_M
    y_back = STRIKE_ZONE_Y_BACK_M

    front_face = [
        [x_left, y_front, z_bottom_m],
        [x_right, y_front, z_bottom_m],
        [x_right, y_front, z_top_m],
        [x_left, y_front, z_top_m],
    ]
    back_face = [
        [x_left, y_back, z_bottom_m],
        [x_right, y_back, z_bottom_m],
        [x_right, y_back, z_top_m],
        [x_left, y_back, z_top_m],
    ]
    connectors = [
        [front_face[0], back_face[0]],
        [front_face[1], back_face[1]],
        [front_face[2], back_face[2]],
        [front_face[3], back_face[3]],
    ]

    x_thirds = [
        x_left + (2.0 * STRIKE_ZONE_X_HALF_M) / 3.0,
        x_left + 2.0 * (2.0 * STRIKE_ZONE_X_HALF_M) / 3.0,
    ]
    z_thirds = [
        z_bottom_m + z_height_m / 3.0,
        z_bottom_m + 2.0 * z_height_m / 3.0,
    ]
    front_grid = [
        [[x, y_front, z_bottom_m], [x, y_front, z_top_m]]
        for x in x_thirds
    ] + [
        [[x_left, y_front, z], [x_right, y_front, z]]
        for z in z_thirds
    ]

    return StrikeZoneGeometry(
        batter_height_cm=height_cm,
        x_half_m=STRIKE_ZONE_X_HALF_M,
        y_front_m=y_front,
        y_back_m=y_back,
        z_bottom_m=z_bottom_m,
        z_top_m=z_top_m,
        z_height_m=z_height_m,
        front_face=front_face,
        back_face=back_face,
        connectors=connectors,
        front_grid=front_grid,
    )
