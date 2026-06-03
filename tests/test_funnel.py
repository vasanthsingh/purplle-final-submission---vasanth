"""Funnel tests — session dedup, re-entry collapse, stage monotonicity."""
from __future__ import annotations

import pytest

from .conftest import build_test_event
from .test_metrics import _utc_today_at


@pytest.mark.asyncio
async def test_reentry_collapses_into_single_session(client):
    ts = _utc_today_at(9)
    events = [
        build_test_event(event_type="ENTRY", visitor_id="V_RE", timestamp=ts),
        build_test_event(event_type="EXIT", visitor_id="V_RE", timestamp=_utc_today_at(9, 10)),
        build_test_event(event_type="REENTRY", visitor_id="V_RE", timestamp=_utc_today_at(9, 30)),
    ]
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200
    r = await client.get("/stores/STORE_001/funnel")
    stages = r.json()["stages"]
    entry = next(s for s in stages if s["stage"] == "Entry")
    assert entry["count"] == 1  # collapsed across REENTRY


@pytest.mark.asyncio
async def test_stage_counts_are_monotonic(client):
    ts = _utc_today_at(10)
    events = []
    for i in range(5):
        vid = f"VIS_{i:06d}"
        events.append(build_test_event(event_type="ENTRY", visitor_id=vid, timestamp=ts))
        if i < 4:
            events.append(build_test_event(event_type="ZONE_ENTER", visitor_id=vid, zone_id="ZONE_MAKEUP", timestamp=_utc_today_at(10, 3)))
        if i < 3:
            events.append(build_test_event(
                event_type="BILLING_QUEUE_JOIN", visitor_id=vid,
                zone_id="ZONE_BILLING", timestamp=_utc_today_at(10, 6),
                metadata={"queue_depth": 1},
            ))
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200

    r = await client.get("/stores/STORE_001/funnel")
    stages = r.json()["stages"]
    counts = [s["count"] for s in stages]
    assert all(counts[i] >= counts[i + 1] for i in range(len(counts) - 1))
    assert counts[0] == 5
    assert counts[1] == 4
    assert counts[2] == 3
    for s in stages:
        assert 0.0 <= s["drop_off_from_prev_pct"] <= 100.0
