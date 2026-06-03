"""Outlet metrics tests — zero defaults, staff exclusion, conversion math."""
from __future__ import annotations

import pytest

from .conftest import build_test_event


@pytest.mark.asyncio
async def test_empty_outlet_returns_zero_defaults(client):
    r = await client.get("/stores/STORE_EMPTY/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body["unique_visitors"] == 0
    assert body["conversion_rate"] == 0.0
    assert body["abandonment_rate"] == 0.0
    assert body["avg_dwell_per_zone_ms"] == {}
    assert body["current_queue_depth"] == 0


@pytest.mark.asyncio
async def test_staff_only_yields_zero_footfall(client):
    today_ts = _utc_today_at(10)
    events = [
        build_test_event(event_type="ENTRY", is_staff=True, timestamp=today_ts, visitor_id=f"STAFF_{i}")
        for i in range(5)
    ]
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200

    r = await client.get("/stores/STORE_001/metrics")
    assert r.status_code == 200
    assert r.json()["unique_visitors"] == 0


@pytest.mark.asyncio
async def test_conversion_with_pos_correlation(client):
    today_ts = _utc_today_at(10)
    vid = "VIS_ABCDE1"
    events = [
        build_test_event(event_type="ENTRY", visitor_id=vid, timestamp=today_ts),
        build_test_event(
            event_type="BILLING_QUEUE_JOIN",
            visitor_id=vid,
            zone_id="ZONE_BILLING",
            timestamp=_utc_today_at(10, 5),
            metadata={"queue_depth": 2},
        ),
    ]
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200

    pos_row = {
        "transaction_id": "TXN_ABC",
        "store_id": "STORE_001",
        "visitor_id": vid,
        "timestamp": _utc_today_at(10, 7),
        "basket_value": 499.0,
        "items_count": 2,
    }
    r = await client.post("/pos/ingest", json={"transactions": [pos_row]})
    assert r.status_code == 200

    r = await client.get("/stores/STORE_001/metrics")
    body = r.json()
    assert body["unique_visitors"] == 1
    assert body["conversion_rate"] == 1.0
    assert body["pos_transactions"] == 1


@pytest.mark.asyncio
async def test_abandonment_rate_calculation(client):
    today_ts = _utc_today_at(10)
    events = [
        build_test_event(event_type="ENTRY", visitor_id="V1", timestamp=today_ts),
        build_test_event(
            event_type="BILLING_QUEUE_JOIN",
            visitor_id="V1",
            zone_id="ZONE_BILLING",
            timestamp=_utc_today_at(10, 5),
            metadata={"queue_depth": 1},
        ),
        build_test_event(
            event_type="BILLING_QUEUE_ABANDON",
            visitor_id="V1",
            zone_id="ZONE_BILLING",
            timestamp=_utc_today_at(10, 12),
        ),
    ]
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200
    r = await client.get("/stores/STORE_001/metrics")
    body = r.json()
    assert body["abandonment_rate"] == 1.0


def _utc_today_at(hour: int, minute: int = 0) -> str:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    ts = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return ts.isoformat()
