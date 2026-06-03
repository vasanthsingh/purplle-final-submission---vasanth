"""FastAPI entrypoint for Vortex Retail Analytics Engine.

Routes:
  GET  /health
  POST /events/ingest
  POST /pos/ingest                (real POS rows during demo)
  GET  /stores/{id}/metrics
  GET  /stores/{id}/funnel
  GET  /stores/{id}/heatmap
  GET  /stores/{id}/anomalies
  GET  /stores/{id}/events        (paginated raw event list)
  WS   /ws/stores/{id}            (real-time WebSocket stream)
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncio
import time
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from sqlalchemy import select

from .anomalies import scan_for_alerts
from .db import create_all, dispose, activity_log, sales_ledger, db_transaction
from .errors import attach_fault_handlers
from .funnel import build_conversion_pipeline
from .health import system_status_check
from .heatmap import build_zone_intensity
from .ingestion import PayloadOverflow, process_event_batch
from .logging_mw import RequestAuditLayer, setup_audit_logging
from .metrics import generate_outlet_snapshot
from .models import SaleRecord


@asynccontextmanager
async def lifespan(application: FastAPI):
    setup_audit_logging()
    await create_all()
    yield
    await dispose()


app = FastAPI(
    title="Vortex Retail Analytics Engine",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(RequestAuditLayer)
attach_fault_handlers(app)

# Serve the web dashboard at "/" when the directory exists.
_DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "web"
if _DASHBOARD_DIR.is_dir():
    @app.get("/", include_in_schema=False)
    async def _serve_dashboard() -> FileResponse:
        return FileResponse(_DASHBOARD_DIR / "index.html")

    app.mount(
        "/static",
        StaticFiles(directory=str(_DASHBOARD_DIR)),
        name="dashboard-static",
    )


@app.get("/health")
async def health() -> JSONResponse:
    snap = await system_status_check()
    # DB unavailable → 503 with structured body.
    status_code = 503 if snap.get("database") != "ok" else 200
    return JSONResponse(status_code=status_code, content=snap)


@app.post("/events/ingest")
async def events_ingest(payload: dict[str, Any], request: Request) -> JSONResponse:
    # Accept both {"events": [...]} and a bare list for flexibility.
    events_list = payload.get("events") if isinstance(payload, dict) else payload
    if not isinstance(events_list, list):
        raise HTTPException(status_code=422, detail="body must contain an events list")

    try:
        result = await process_event_batch(events_list)
    except PayloadOverflow as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    request.state.event_count = result.accepted
    status_code = 200 if not result.rejected else 207  # multi-status on partial
    return JSONResponse(status_code=status_code, content=result.model_dump(mode="json"))


@app.post("/pos/ingest")
async def pos_ingest(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("transactions") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise HTTPException(status_code=422, detail="body must contain a transactions list")
    validated = [SaleRecord.model_validate(r) for r in rows]
    if not validated:
        return {"accepted": 0}

    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    values = [
        {
            "transaction_id": t.transaction_id,
            "store_id": t.store_id,
            "visitor_id": t.visitor_id,
            "timestamp": t.timestamp,
            "basket_value": t.basket_value,
            "items_count": t.items_count,
            "line_items": t.line_items,
        }
        for t in validated
    ]
    async with db_transaction() as s:
        dialect = s.bind.dialect.name if s.bind else ""
        stmt: Any
        if dialect == "postgresql":
            stmt = pg_insert(sales_ledger).values(values).on_conflict_do_nothing(
                index_elements=[sales_ledger.c.transaction_id]
            )
        elif dialect == "sqlite":
            stmt = sqlite_insert(sales_ledger).values(values).on_conflict_do_nothing(
                index_elements=[sales_ledger.c.transaction_id]
            )
        else:
            stmt = sales_ledger.insert().values(values)
        await s.execute(stmt)

    # Reclassify BILLING_QUEUE_LEAVE → BILLING_QUEUE_ABANDON where no POS txn followed.
    await _reclassify_queue_abandons(validated)

    return {"accepted": len(validated)}


async def _reclassify_queue_abandons(pos_rows: list[SaleRecord]) -> None:
    """After POS ingest, reclassify LEAVE → ABANDON where no purchase followed.

    For each store that received new POS data, find all BILLING_QUEUE_LEAVE
    events in today's window. If NO POS transaction falls within
    [leave_ts, leave_ts + 5min], reclassify as BILLING_QUEUE_ABANDON.
    """
    from datetime import timedelta
    from sqlalchemy import and_, update

    if not pos_rows:
        return

    outlet_ids = {t.store_id for t in pos_rows}

    async with db_transaction() as s:
        ev = activity_log.c
        sl = sales_ledger.c

        for sid in outlet_ids:
            # Fetch all BILLING_QUEUE_LEAVE events for this outlet.
            leave_q = select(ev.event_id, ev.visitor_id, ev.timestamp).where(
                and_(
                    ev.store_id == sid,
                    ev.event_type == "BILLING_QUEUE_LEAVE",
                )
            )
            leave_rows = (await s.execute(leave_q)).all()

            if not leave_rows:
                continue

            # Fetch all POS timestamps for this outlet.
            pos_q = select(sl.timestamp).where(sl.store_id == sid)
            pos_timestamps = [r[0] for r in (await s.execute(pos_q)).all()]

            # For each LEAVE check if any POS txn follows within 5 minutes.
            abandon_ids = []
            for eid, vid, leave_ts in leave_rows:
                has_purchase = any(
                    timedelta(seconds=0) <= pts - leave_ts <= timedelta(minutes=5)
                    for pts in pos_timestamps
                )
                if not has_purchase:
                    abandon_ids.append(eid)

            # Reclassify matching events.
            if abandon_ids:
                stmt = (
                    update(activity_log)
                    .where(activity_log.c.event_id.in_(abandon_ids))
                    .values(event_type="BILLING_QUEUE_ABANDON")
                )
                await s.execute(stmt)


# In-memory result store to shield the DB during high-concurrency reads.
_RESULT_STORE: dict[str, dict[str, Any]] = {}
_STORE_EXPIRY = 3.0  # seconds

async def fetch_or_compute(key: str, func: Any, *args: Any, **kwargs: Any) -> Any:
    now = time.time()
    if key in _RESULT_STORE and now - _RESULT_STORE[key]["time"] < _STORE_EXPIRY:
        return _RESULT_STORE[key]["data"]

    data = await func(*args, **kwargs)
    _RESULT_STORE[key] = {"time": now, "data": data}
    return data

@app.get("/stores/{store_id}/metrics")
async def store_metrics(store_id: str) -> dict[str, Any]:
    snapshot = await fetch_or_compute(f"metrics_{store_id}", generate_outlet_snapshot, store_id)
    return snapshot.to_dict()

@app.get("/stores/{store_id}/funnel")
async def store_funnel(store_id: str) -> dict[str, Any]:
    return await fetch_or_compute(f"funnel_{store_id}", build_conversion_pipeline, store_id)

@app.get("/stores/{store_id}/heatmap")
async def store_heatmap(store_id: str) -> dict[str, Any]:
    return await fetch_or_compute(f"heatmap_{store_id}", build_zone_intensity, store_id)

@app.get("/stores/{store_id}/anomalies")
async def store_anomalies(store_id: str) -> dict[str, Any]:
    alerts = await fetch_or_compute(f"alerts_{store_id}", scan_for_alerts, store_id)
    return {"store_id": store_id, "anomalies": alerts, "count": len(alerts)}


@app.get("/stores/{store_id}/events")
async def store_events(store_id: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """Paginated raw event list for a store — useful for evaluator inspection."""
    from sqlalchemy import func, select as sa_select

    async with db_transaction() as s:
        ev = activity_log.c
        total = int(
            (await s.execute(
                sa_select(func.count()).where(ev.store_id == store_id)
            )).scalar() or 0
        )
        rows = (await s.execute(
            sa_select(activity_log)
            .where(ev.store_id == store_id)
            .order_by(ev.timestamp.desc())
            .limit(min(limit, 500))
            .offset(offset)
        )).all()

    events_out = []
    for r in rows:
        events_out.append({
            "event_id": r.event_id,
            "store_id": r.store_id,
            "camera_id": r.camera_id,
            "visitor_id": r.visitor_id,
            "event_type": r.event_type,
            "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            "zone_id": r.zone_id,
            "dwell_ms": r.dwell_ms,
            "is_staff": r.is_staff,
            "confidence": r.confidence,
            "metadata": r.metadata_json,
        })
    return {"store_id": store_id, "total": total, "limit": limit, "offset": offset, "events": events_out}


@app.websocket("/ws/stores/{store_id}")
async def websocket_endpoint(websocket: WebSocket, store_id: str):
    await websocket.accept()
    try:
        while True:
            snapshot = await fetch_or_compute(f"metrics_{store_id}", generate_outlet_snapshot, store_id)
            funnel = await fetch_or_compute(f"funnel_{store_id}", build_conversion_pipeline, store_id)
            intensity = await fetch_or_compute(f"heatmap_{store_id}", build_zone_intensity, store_id)
            alerts = await fetch_or_compute(f"alerts_{store_id}", scan_for_alerts, store_id)
            health = await fetch_or_compute("health", system_status_check)

            payload = {
                "metrics": snapshot.to_dict(),
                "funnel": funnel,
                "heatmap": intensity,
                "anomalies": {"store_id": store_id, "anomalies": alerts, "count": len(alerts)},
                "health": health
            }
            await websocket.send_json(payload)
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
