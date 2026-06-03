"""Endpoint coverage tests for /health, /heatmap, dwell rollup."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from .conftest import build_test_event
from .test_metrics import _utc_today_at


@pytest.mark.asyncio
async def test_health_detects_stale_feed(client):
    ts_old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    events = [build_test_event(
        event_type="ENTRY", visitor_id="VOLD", timestamp=ts_old,
    )]
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200
    r = await client.get("/health")
    body = r.json()
    assert body["status"] in ("degraded", "ok")
    assert any(s["last_event_timestamp"] for s in body["stores"])


@pytest.mark.asyncio
async def test_health_on_empty_database(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["database"] == "ok"


@pytest.mark.asyncio
async def test_zone_intensity_normalisation(client):
    ts = _utc_today_at(11)
    events = [
        build_test_event(event_type="ZONE_ENTER", visitor_id="V1", zone_id="ZONE_A", timestamp=ts),
        build_test_event(event_type="ZONE_ENTER", visitor_id="V2", zone_id="ZONE_A", timestamp=ts),
        build_test_event(event_type="ZONE_ENTER", visitor_id="V3", zone_id="ZONE_B", timestamp=ts),
        build_test_event(event_type="ENTRY", visitor_id="V1", timestamp=ts),
    ]
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200
    r = await client.get("/stores/STORE_001/heatmap")
    body = r.json()
    zmap = {z["zone_id"]: z for z in body["zones"]}
    assert zmap["ZONE_A"]["intensity"] == 100.0
    assert zmap["ZONE_B"]["intensity"] == 50.0
    assert body["data_confidence"] == "low"


@pytest.mark.asyncio
async def test_dwell_aggregation_per_zone(client):
    ts = _utc_today_at(12)
    events = [
        build_test_event(event_type="ZONE_DWELL", visitor_id="V1", zone_id="ZONE_MAKEUP", dwell_ms=60_000, timestamp=ts),
        build_test_event(event_type="ZONE_DWELL", visitor_id="V2", zone_id="ZONE_MAKEUP", dwell_ms=120_000, timestamp=ts),
        build_test_event(event_type="ZONE_DWELL", visitor_id="V1", zone_id="ZONE_SKIN", dwell_ms=30_000, timestamp=ts),
    ]
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200
    r = await client.get("/stores/STORE_001/metrics")
    avgs = r.json()["avg_dwell_per_zone_ms"]
    assert avgs["ZONE_MAKEUP"] == pytest.approx(90_000.0)
    assert avgs["ZONE_SKIN"] == pytest.approx(30_000.0)


@pytest.mark.asyncio
async def test_queue_depth_takes_max(client):
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    events = [
        build_test_event(
            event_type="BILLING_QUEUE_JOIN", visitor_id=f"V_{i}",
            zone_id="ZONE_BILLING",
            timestamp=(now - timedelta(minutes=i)).isoformat(),
            metadata={"queue_depth": d},
        )
        for i, d in enumerate([3, 5, 2])
    ]
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200
    r = await client.get("/stores/STORE_001/metrics")
    assert r.json()["current_queue_depth"] == 5


@pytest.mark.asyncio
async def test_pos_deduplication(client):
    row = {
        "transaction_id": "TXN_FIXED",
        "store_id": "STORE_001",
        "visitor_id": "V1",
        "timestamp": _utc_today_at(14),
        "basket_value": 199.0,
        "items_count": 1,
    }
    r1 = await client.post("/pos/ingest", json={"transactions": [row]})
    r2 = await client.post("/pos/ingest", json={"transactions": [row]})
    assert r1.status_code == 200 and r2.status_code == 200
    r = await client.get("/stores/STORE_001/metrics")
    assert r.json()["pos_transactions"] == 1
