"""WebSocket, event listing, POS reclassification, and additional endpoint tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from .conftest import build_test_event
from .test_metrics import _utc_today_at


@pytest.mark.asyncio
async def test_event_listing_returns_ingested(client):
    events = [build_test_event(visitor_id=f"V_{i}") for i in range(3)]
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200

    r = await client.get("/stores/STORE_001/events?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert len(body["events"]) == 3
    assert body["store_id"] == "STORE_001"


@pytest.mark.asyncio
async def test_event_listing_pagination(client):
    events = [build_test_event(visitor_id=f"V_{i}") for i in range(5)]
    await client.post("/events/ingest", json={"events": events})

    r = await client.get("/stores/STORE_001/events?limit=2&offset=0")
    assert r.status_code == 200
    assert len(r.json()["events"]) == 2

    r = await client.get("/stores/STORE_001/events?limit=2&offset=4")
    assert r.status_code == 200
    assert len(r.json()["events"]) == 1


@pytest.mark.asyncio
async def test_event_listing_empty_outlet(client):
    r = await client.get("/stores/STORE_EMPTY/events")
    assert r.status_code == 200
    assert r.json()["total"] == 0
    assert r.json()["events"] == []


@pytest.mark.asyncio
async def test_leave_reclassified_to_abandon(client):
    """BILLING_QUEUE_LEAVE without a matching POS txn should be reclassified to ABANDON."""
    ts = _utc_today_at(11)

    events = [
        build_test_event(event_type="ENTRY", visitor_id="V_RECLASS", timestamp=ts),
        build_test_event(
            event_type="BILLING_QUEUE_JOIN", visitor_id="V_RECLASS",
            zone_id="ZONE_BILLING", timestamp=_utc_today_at(11, 5),
            metadata={"queue_depth": 1},
        ),
        build_test_event(
            event_type="BILLING_QUEUE_LEAVE", visitor_id="V_RECLASS",
            zone_id="ZONE_BILLING", timestamp=_utc_today_at(11, 8),
        ),
    ]
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200

    pos = {
        "transaction_id": "TXN_FAR_AWAY",
        "store_id": "STORE_001",
        "visitor_id": "V_OTHER",
        "timestamp": _utc_today_at(11, 30),
        "basket_value": 500.0,
        "items_count": 1,
    }
    r = await client.post("/pos/ingest", json={"transactions": [pos]})
    assert r.status_code == 200

    r = await client.get("/stores/STORE_001/events?limit=100")
    evts = r.json()["events"]
    leave_events = [e for e in evts if e["event_type"] == "BILLING_QUEUE_LEAVE"]
    abandon_events = [e for e in evts if e["event_type"] == "BILLING_QUEUE_ABANDON"]
    assert len(leave_events) == 0, "LEAVE should have been reclassified"
    assert len(abandon_events) == 1, "Should have exactly one ABANDON"


@pytest.mark.asyncio
async def test_empty_funnel_returns_zero_stages(client):
    r = await client.get("/stores/STORE_EMPTY_FUNNEL/funnel")
    assert r.status_code == 200
    body = r.json()
    assert body["total_sessions"] == 0
    assert body["conversion_rate"] == 0.0
    assert all(s["count"] == 0 for s in body["stages"])


@pytest.mark.asyncio
async def test_full_funnel_with_pos_correlation(client):
    """Full pipeline: Entry → ZoneVisit → BillingQueue → Purchase via POS correlation."""
    ts = _utc_today_at(10)
    vid = "V_FULL_FUNNEL"
    events = [
        build_test_event(event_type="ENTRY", visitor_id=vid, timestamp=ts),
        build_test_event(
            event_type="ZONE_ENTER", visitor_id=vid,
            zone_id="ZONE_SKIN", timestamp=_utc_today_at(10, 3),
        ),
        build_test_event(
            event_type="BILLING_QUEUE_JOIN", visitor_id=vid,
            zone_id="ZONE_BILLING", timestamp=_utc_today_at(10, 8),
            metadata={"queue_depth": 1},
        ),
    ]
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200

    pos = {
        "transaction_id": "TXN_FULL",
        "store_id": "STORE_001",
        "visitor_id": vid,
        "timestamp": _utc_today_at(10, 10),
        "basket_value": 999.0,
        "items_count": 3,
    }
    r = await client.post("/pos/ingest", json={"transactions": [pos]})
    assert r.status_code == 200

    r = await client.get("/stores/STORE_001/funnel")
    body = r.json()
    stages = {s["stage"]: s["count"] for s in body["stages"]}
    assert stages["Entry"] == 1
    assert stages["ZoneVisit"] == 1
    assert stages["BillingQueue"] == 1
    assert stages["Purchase"] == 1
    assert body["conversion_rate"] == 1.0


@pytest.mark.asyncio
async def test_staff_count_tracked_separately(client):
    ts = _utc_today_at(10)
    events = [
        build_test_event(event_type="ENTRY", visitor_id="V1", timestamp=ts),
        build_test_event(event_type="ENTRY", visitor_id="STAFF1", timestamp=ts, is_staff=True),
        build_test_event(event_type="ENTRY", visitor_id="STAFF2", timestamp=ts, is_staff=True),
    ]
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200

    r = await client.get("/stores/STORE_001/metrics")
    body = r.json()
    assert body["unique_visitors"] == 1
    assert body["staff_count"] == 2
