"""System health probe — per-outlet last_event_timestamp and STALE_FEED warning."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from .config import APP_CONFIG
from .db import activity_log, get_engine, db_transaction


async def system_status_check(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(minutes=APP_CONFIG.stale_feed_minutes)
    db_ok = True
    outlets: list[dict[str, Any]] = []
    try:
        async with db_transaction() as s:
            col = activity_log.c
            rows = (
                await s.execute(
                    select(col.store_id, func.max(col.timestamp)).group_by(col.store_id)
                )
            ).all()
        for outlet_id, last_ts in rows:
            if last_ts is not None and last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            last_iso = last_ts.isoformat() if last_ts else None
            stale = bool(last_ts and last_ts < stale_cutoff)
            outlets.append(
                {
                    "store_id": outlet_id,
                    "last_event_timestamp": last_iso,
                    "stale": stale,
                }
            )
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        import structlog
        structlog.get_logger().warning("health_db_probe_failed", error_class=type(exc).__name__, error=str(exc))

    warnings = [f"STALE_FEED: {o['store_id']}" for o in outlets if o.get("stale")]
    status = "ok" if db_ok and not warnings else ("degraded" if db_ok else "db_unavailable")

    return {
        "status": status,
        "database": "ok" if db_ok else "unavailable",
        "engine": str(get_engine().url.drivername) if db_ok else None,
        "checked_at": now.isoformat(),
        "stores": outlets,
        "warnings": warnings,
    }
