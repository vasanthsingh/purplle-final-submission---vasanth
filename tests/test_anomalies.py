"""Anomaly tests — severity tiers, dead zone, insufficient history handling."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from .conftest import build_test_event


def _relative_ts(ago_seconds: int) -> str:
    now = datetime.now(timezone.utc)
    return (now - timedelta(seconds=ago_seconds)).isoformat()


@pytest.mark.asyncio
async def test_queue_spike_emits_critical(client):
    events = []
    for i in range(3):
        events.append(build_test_event(
            event_type="BILLING_QUEUE_JOIN",
            visitor_id=f"VIS_{i}",
            zone_id="ZONE_BILLING",
            timestamp=_relative_ts(30 + i * 10),
            metadata={"queue_depth": 9},
        ))
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200, r.text

    r = await client.get("/stores/STORE_001/anomalies")
    body = r.json()
    spikes = [a for a in body["anomalies"] if a["type"] == "BILLING_QUEUE_SPIKE"]
    assert spikes, body
    assert spikes[0]["severity"] == "CRITICAL"
    assert "suggested_action" in spikes[0]
    assert spikes[0]["suggested_action"]


@pytest.mark.asyncio
async def test_conversion_drop_insufficient_history_yields_info(client):
    # Single entry today — no trailing 7-day history.
    events = [build_test_event(
        event_type="ENTRY", visitor_id="V1", timestamp=_relative_ts(60),
    )]
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200
    r = await client.get("/stores/STORE_001/anomalies")
    kinds = [a for a in r.json()["anomalies"] if a["type"] == "CONVERSION_DROP"]
    # Insufficient history → INFO (not CRITICAL/WARN).
    for a in kinds:
        assert a["severity"] == "INFO"


@pytest.mark.asyncio
async def test_clean_outlet_has_zero_alerts(client):
    r = await client.get("/stores/STORE_VIRGIN/anomalies")
    assert r.status_code == 200
    assert r.json()["count"] == 0
