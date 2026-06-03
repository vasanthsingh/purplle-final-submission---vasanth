"""Pipeline / schema tests — geometry utilities, dispatch, caching, dedup."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest

from pipeline.emit import EventDispatcher, DispatcherSettings, create_activity
from pipeline.reentry import ReturnVisitorBuffer, vector_similarity
from pipeline.zones import (
    BoundaryDetector,
    RegionTracker,
    bounding_box_midpoint,
    line_side,
    is_point_inside_region,
)


def test_convex_polygon_containment():
    square = [(0, 0), (10, 0), (10, 10), (0, 10)]
    assert is_point_inside_region((5, 5), square) is True
    assert is_point_inside_region((11, 11), square) is False


def test_concave_polygon_containment():
    concave = [(0, 0), (10, 0), (10, 10), (5, 5), (0, 10)]
    assert is_point_inside_region((5, 9), concave) is False
    assert is_point_inside_region((5, 2), concave) is True


def test_boundary_crossing_direction():
    bd = BoundaryDetector(a=(0, 5), b=(10, 5), inside_normal=(0, 1))
    assert bd.update("v1", (5, 1)) is None  # first observation
    evt = bd.update("v1", (5, 9))
    assert evt in ("enter", "exit")
    evt2 = bd.update("v1", (5, 1))
    assert evt2 in ("enter", "exit") and evt2 != evt


def test_region_tracker_emits_enter_dwell_exit():
    rt = RegionTracker()
    out1 = rt.on_zone_event("v1", "z1", True, 0)
    assert out1 == [("ZONE_ENTER", 0)]
    # no re-enter within 30s should produce no event
    assert rt.on_zone_event("v1", "z1", True, 1_000) == []
    # after 30s we get a DWELL
    dwell = rt.on_zone_event("v1", "z1", True, 31_000)
    assert dwell and dwell[0][0] == "ZONE_DWELL"
    exit_out = rt.on_zone_event("v1", "z1", False, 32_000)
    assert exit_out == [("ZONE_EXIT", 32_000)]


def test_return_buffer_evicts_past_window():
    buf = ReturnVisitorBuffer(window_ms=1000, similarity_threshold=0.5)
    buf.record_exit("V1", (1.0, 0.0, 0.0), ts_ms=0)
    assert buf.lookup((1.0, 0.0, 0.0), ts_ms=500) == "V1"
    # past the window
    assert buf.lookup((1.0, 0.0, 0.0), ts_ms=2000) is None


def test_vector_similarity_extremes():
    assert vector_similarity((1.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)
    assert vector_similarity((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)


def test_midpoint_calculation():
    assert bounding_box_midpoint((0, 0, 10, 20)) == (5.0, 10.0)


def test_line_side_sign_opposition():
    above = line_side((0, 10), (0, 0), (10, 0))
    below = line_side((0, -10), (0, 0), (10, 0))
    assert (above > 0) != (below > 0)
    assert above != 0 and below != 0


def test_dispatcher_writes_and_posts(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    received: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        received.append(len(body["events"]))
        return httpx.Response(200, json={"accepted": len(body["events"]), "duplicates": 0, "rejected": []})

    cfg = DispatcherSettings(api_url="http://api", jsonl_path=path, batch_size=2)
    dispatcher = EventDispatcher(cfg)
    dispatcher._http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        from datetime import datetime, timezone
        for i in range(5):
            dispatcher.emit(create_activity(
                store_id="S", camera_id="C", visitor_id=f"V_{i}",
                event_type="ENTRY", timestamp=datetime.now(timezone.utc),
            ))
        dispatcher.flush()
    finally:
        dispatcher.close()

    # JSONL should have all 5 lines.
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 5
    # All UUIDs unique.
    ids = [json.loads(ln)["event_id"] for ln in lines]
    assert len(set(ids)) == 5
    # Two full batches of 2 and one final flush of 1.
    assert sum(received) == 5


def test_activity_uuid_uniqueness():
    from datetime import datetime, timezone
    ids = {
        create_activity(
            store_id="S", camera_id="C", visitor_id="V",
            event_type="ENTRY", timestamp=datetime.now(timezone.utc),
        )["event_id"]
        for _ in range(1000)
    }
    assert len(ids) == 1000


def test_activity_schema_matches_pydantic():
    from datetime import datetime, timezone
    from app.models import BehaviourEvent
    raw = create_activity(
        store_id="S", camera_id="C", visitor_id="V",
        event_type="ENTRY", timestamp=datetime.now(timezone.utc),
        confidence=0.7,
    )
    raw["event_id"] = str(uuid.UUID(raw["event_id"]))
    BehaviourEvent.model_validate(raw)  # must not raise


def test_overlap_filter_suppresses_duplicate():
    from pipeline.cross_camera import OverlapFilter
    filt = OverlapFilter(window_ms=3000)
    assert filt.should_emit("V1", "ZONE_SKIN", 1000) is True
    assert filt.should_emit("V1", "ZONE_SKIN", 2000) is False
    assert filt.should_emit("V1", "ZONE_MAKEUP", 2000) is True
    assert filt.should_emit("V1", "ZONE_SKIN", 5000) is True


def test_overlap_filter_prune():
    from pipeline.cross_camera import OverlapFilter
    filt = OverlapFilter(window_ms=1000)
    filt.should_emit("V1", "Z1", 100)
    filt.should_emit("V2", "Z2", 200)
    assert len(filt._last_seen) == 2
    filt.prune(50_000)
    assert len(filt._last_seen) == 0
