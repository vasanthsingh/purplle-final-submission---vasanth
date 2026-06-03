"""Alert scanning: billing queue spike, conversion drop, dead zone, stale camera.

Severity levels: CRITICAL | WARN | INFO. Each alert carries a suggested_action.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, select

from .config import APP_CONFIG
from .db import activity_log, sales_ledger, db_transaction
from .metrics import _current_day_range


async def scan_for_alerts(store_id: str, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    day_start, day_end = _current_day_range(now)
    ev = activity_log.c
    sl = sales_ledger.c
    alerts: list[dict[str, Any]] = []

    async with db_transaction() as s:
        # --- queue spike --------------------------------------------------
        spike_cutoff = now - timedelta(seconds=APP_CONFIG.queue_spike_duration_sec)
        q_rows = (
            await s.execute(
                select(ev.timestamp, ev.metadata_json).where(
                    and_(
                        ev.store_id == store_id,
                        ev.event_type == "BILLING_QUEUE_JOIN",
                        ev.timestamp >= spike_cutoff,
                        ev.is_staff.is_(False),
                    )
                )
            )
        ).all()
        sustained = [
            (ts, int(md.get("queue_depth", 0)))
            for ts, md in q_rows
            if isinstance(md, dict) and int(md.get("queue_depth", 0)) > APP_CONFIG.queue_spike_threshold
        ]
        if len(sustained) >= 2:  # at least 2 samples above threshold
            max_depth = max(d for _, d in sustained)
            alerts.append(
                {
                    "type": "BILLING_QUEUE_SPIKE",
                    "severity": "CRITICAL",
                    "store_id": store_id,
                    "detected_at": now.isoformat(),
                    "detail": {
                        "max_queue_depth": max_depth,
                        "threshold": APP_CONFIG.queue_spike_threshold,
                        "window_seconds": APP_CONFIG.queue_spike_duration_sec,
                    },
                    "suggested_action": "Open a second billing counter immediately.",
                }
            )

        # --- conversion drop ---------------------------------------------
        today_conv = await _compute_conversion(s, store_id, day_start, day_end)
        trailing_start = day_start - timedelta(days=APP_CONFIG.trailing_days)
        trailing_conv = await _compute_conversion(s, store_id, trailing_start, day_start)
        has_trailing = trailing_conv is not None
        if has_trailing and trailing_conv and today_conv is not None:
            if today_conv < 0.70 * trailing_conv:
                alerts.append(
                    {
                        "type": "CONVERSION_DROP",
                        "severity": "WARN",
                        "store_id": store_id,
                        "detected_at": now.isoformat(),
                        "detail": {
                            "today_conversion": round(today_conv, 4),
                            "trailing_avg": round(trailing_conv, 4),
                            "trailing_days": APP_CONFIG.trailing_days,
                        },
                        "suggested_action": "Review staffing and promotions for today.",
                    }
                )
        elif today_conv is not None and not has_trailing:
            alerts.append(
                {
                    "type": "CONVERSION_DROP",
                    "severity": "INFO",
                    "store_id": store_id,
                    "detected_at": now.isoformat(),
                    "detail": {
                        "today_conversion": round(today_conv or 0, 4),
                        "note": "Insufficient trailing history to benchmark.",
                    },
                    "suggested_action": "Collect more data before triggering alerts.",
                }
            )

        # --- dead zone ----------------------------------------------------
        dead_cutoff = now - timedelta(seconds=APP_CONFIG.dead_zone_window_sec)
        active_zones_q = (
            select(func.distinct(ev.zone_id))
            .where(
                and_(
                    ev.store_id == store_id,
                    ev.event_type == "ZONE_ENTER",
                    ev.timestamp >= dead_cutoff,
                )
            )
        )
        active = {r[0] for r in (await s.execute(active_zones_q)).all() if r[0]}

        all_zones_q = (
            select(func.distinct(ev.zone_id))
            .where(
                and_(
                    ev.store_id == store_id,
                    ev.timestamp >= day_start,
                    ev.zone_id.isnot(None),
                )
            )
        )
        all_zones = {r[0] for r in (await s.execute(all_zones_q)).all()}
        cfg_ref = APP_CONFIG  # readability
        silent = all_zones - active
        if _is_store_open(now) and silent:
            for z in sorted(silent):
                alerts.append(
                    {
                        "type": "DEAD_ZONE",
                        "severity": "INFO",
                        "store_id": store_id,
                        "detected_at": now.isoformat(),
                        "detail": {
                            "zone_id": z,
                            "silent_seconds": APP_CONFIG.dead_zone_window_sec,
                        },
                        "suggested_action": f"Inspect zone {z} for obstructions or camera failure.",
                    }
                )

        _ = cfg_ref  # avoid unused warning
        _ = sl  # reserved for future POS-based alerts

        # --- stale camera -------------------------------------------------
        stale_cutoff = now - timedelta(minutes=10)
        cam_q = (
            select(ev.camera_id, func.max(ev.timestamp).label("last_ts"))
            .where(ev.store_id == store_id)
            .group_by(ev.camera_id)
        )
        for row in (await s.execute(cam_q)).all():
            cam_id, last_ts = row.camera_id, row.last_ts
            if last_ts:
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
                if last_ts < stale_cutoff:
                    alerts.append(
                        {
                            "type": "STALE_CAMERA",
                            "severity": "CRITICAL",
                            "store_id": store_id,
                            "detected_at": now.isoformat(),
                            "detail": {
                                "camera_id": cam_id,
                                "last_event_at": last_ts.isoformat(),
                                "silent_minutes": round((now - last_ts).total_seconds() / 60),
                            },
                            "suggested_action": f"Check connectivity for {cam_id}; restart edge agent.",
                        }
                    )

    return alerts


async def _compute_conversion(session, store_id: str, start: datetime, end: datetime) -> float | None:
    ev = activity_log.c
    sl = sales_ledger.c
    uv = (
        await session.execute(
            select(func.count(func.distinct(ev.visitor_id))).where(
                and_(
                    ev.store_id == store_id,
                    ev.timestamp >= start,
                    ev.timestamp < end,
                    ev.event_type == "ENTRY",
                    ev.is_staff.is_(False),
                )
            )
        )
    ).scalar() or 0
    if uv == 0:
        return None
    pos_q = select(sl.timestamp).where(
        and_(sl.store_id == store_id, sl.timestamp >= start, sl.timestamp < end)
    )
    pos_timestamps = [r[0] for r in (await session.execute(pos_q)).all()]

    bq_q = select(ev.visitor_id, ev.timestamp).where(
        and_(
            ev.store_id == store_id,
            ev.timestamp >= start,
            ev.timestamp < end,
            ev.event_type == "BILLING_QUEUE_JOIN",
            ev.is_staff.is_(False),
        )
    )
    bq_rows = (await session.execute(bq_q)).all()

    converted = set()
    for vid, join_ts in bq_rows:
        if vid in converted:
            continue
        for pts in pos_timestamps:
            if timedelta(seconds=0) <= pts - join_ts <= timedelta(minutes=5):
                converted.add(vid)
                break

    return len(converted) / uv


def _is_store_open(now: datetime) -> bool:
    # No store hours in the DB — default to always-open for the demo.
    _ = now
    return True
