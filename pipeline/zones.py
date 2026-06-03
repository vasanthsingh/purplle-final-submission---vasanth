"""Region geometry, boundary detection, and dwell timers.

Pure-Python with no CV/torch dependencies for cheap unit testing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


Coord = tuple[float, float]


def is_point_inside_region(pt: Coord, polygon: list[Coord]) -> bool:
    """Ray-casting point-in-polygon test. O(n), tolerant of open polygons."""
    if len(polygon) < 3:
        return False
    x, y = pt
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        intersect = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi
        )
        if intersect:
            inside = not inside
        j = i
    return inside


def line_side(pt: Coord, a: Coord, b: Coord) -> float:
    """Signed scalar — positive on one side of the line, negative on the other."""
    return (b[0] - a[0]) * (pt[1] - a[1]) - (b[1] - a[1]) * (pt[0] - a[0])


@dataclass
class BoundaryDetector:
    """Monitors which side of a boundary each visitor was last observed on.

    Returns 'enter' / 'exit' / None when the signed side changes.
    Crossing direction is governed by `inside_normal` (dot product test).
    """

    a: Coord
    b: Coord
    inside_normal: tuple[float, float] = (0.0, 1.0)
    _prev_side: dict[str, float] = field(default_factory=dict)

    def update(self, visitor_id: str, pt: Coord) -> Optional[str]:
        side = line_side(pt, self.a, self.b)
        prev = self._prev_side.get(visitor_id)
        self._prev_side[visitor_id] = side
        if prev is None:
            return None
        if prev == 0 or side == 0:
            return None
        if (prev < 0) == (side < 0):
            return None  # same side, no crossing
        # Crossing happened — determine direction.
        inside_score = side * (self.inside_normal[0] + self.inside_normal[1])
        return "enter" if inside_score > 0 else "exit"


@dataclass
class RegionTracker:
    """Tracks per-(visitor, zone) presence and dwell accumulation."""

    in_region_since_ms: dict[tuple[str, str], int] = field(default_factory=dict)
    last_dwell_emit_ms: dict[tuple[str, str], int] = field(default_factory=dict)

    def on_zone_event(
        self,
        visitor_id: str,
        zone_id: str,
        is_inside: bool,
        timestamp_ms: int,
    ) -> list[tuple[str, int]]:
        """Produce a list of (event_type, dwell_ms) tuples to dispatch."""
        key = (visitor_id, zone_id)
        out: list[tuple[str, int]] = []
        currently_in = key in self.in_region_since_ms

        if is_inside and not currently_in:
            self.in_region_since_ms[key] = timestamp_ms
            self.last_dwell_emit_ms[key] = timestamp_ms
            out.append(("ZONE_ENTER", 0))
        elif is_inside and currently_in:
            start = self.in_region_since_ms[key]
            last_emit = self.last_dwell_emit_ms[key]
            if timestamp_ms - last_emit >= 30_000:  # 30s cadence
                out.append(("ZONE_DWELL", timestamp_ms - start))
                self.last_dwell_emit_ms[key] = timestamp_ms
        elif not is_inside and currently_in:
            start = self.in_region_since_ms.pop(key)
            self.last_dwell_emit_ms.pop(key, None)
            out.append(("ZONE_EXIT", timestamp_ms - start))
        return out


def bounding_box_midpoint(bbox: tuple[float, float, float, float]) -> Coord:
    """(x1, y1, x2, y2) → (cx, cy)."""
    x1, y1, x2, y2 = bbox
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
