"""Ingestion tests — idempotency, validation, partial success, batch limits."""
from __future__ import annotations

import uuid

import pytest

from .conftest import build_test_event


@pytest.mark.asyncio
async def test_single_record_intake(client):
    payload = {"events": [build_test_event()]}
    r = await client.post("/events/ingest", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] == 1
    assert body["duplicates"] == 0
    assert body["rejected"] == []


@pytest.mark.asyncio
async def test_duplicate_rejection(client):
    eid = str(uuid.uuid4())
    payload = {"events": [build_test_event(event_id=eid)]}
    r1 = await client.post("/events/ingest", json=payload)
    r2 = await client.post("/events/ingest", json=payload)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["accepted"] == 1
    assert r2.json()["accepted"] == 0
    assert r2.json()["duplicates"] == 1


@pytest.mark.asyncio
async def test_partial_success_with_invalid_record(client):
    good = build_test_event()
    bad = build_test_event()
    bad["event_type"] = "TOTALLY_BOGUS"
    r = await client.post("/events/ingest", json={"events": [good, bad]})
    assert r.status_code == 207, r.text
    body = r.json()
    assert body["accepted"] == 1
    assert len(body["rejected"]) == 1
    assert "event_type" in body["rejected"][0]["error"]


@pytest.mark.asyncio
async def test_max_batch_of_500(client):
    events = [build_test_event() for _ in range(500)]
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200
    assert r.json()["accepted"] == 500


@pytest.mark.asyncio
async def test_oversized_batch_rejected(client):
    events = [build_test_event() for _ in range(501)]
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_malformed_body_returns_422(client):
    r = await client.post("/events/ingest", json={"wrong": "shape"})
    assert r.status_code == 422
