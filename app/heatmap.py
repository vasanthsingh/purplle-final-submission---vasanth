"""Zone intensity map — visit_count, avg dwell, normalised intensity [0,100].

Flags low confidence when total sessions < 20.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, select

from .db import activity_log, db_transaction
from .metrics import _current_day_range


async def build_zone_intensity(store_id: str, now: datetime | None = None) -> dict[str, Any]:
    day_start, day_end = _current_day_range(now)
    ev = activity_log.c

    async with db_transaction() as s:
        # Entry count for confidence denominator.
        entry_q = select(func.count(func.distinct(ev.visitor_id))).where(
            and_(
                ev.store_id == store_id,
                ev.timestamp >= day_start,
                ev.timestamp < day_end,
                ev.event_type == "ENTRY",
                ev.is_staff.is_(False),
            )
        )
        total_entries = int((await s.execute(entry_q)).scalar() or 0)

        # Per-zone visit counts from ZONE_ENTER/ZONE_DWELL events.
        zone_q = (
            select(ev.zone_id, func.count(), func.avg(ev.dwell_ms))
            .where(
                and_(
                    ev.store_id == store_id,
                    ev.timestamp >= day_start,
                    ev.timestamp < day_end,
                    ev.event_type.in_(["ZONE_ENTER", "ZONE_DWELL"]),
                    ev.is_staff.is_(False),
                    ev.zone_id.isnot(None),
                )
            )
            .group_by(ev.zone_id)
        )
        rows = (await s.execute(zone_q)).all()

    zones = [
        {
            "zone_id": r[0],
            "visit_count": int(r[1] or 0),
            "avg_dwell_ms": round(float(r[2] or 0), 2),
        }
        for r in rows
    ]

    peak_visits = max((z["visit_count"] for z in zones), default=0)
    for z in zones:
        z["intensity"] = round((z["visit_count"] / peak_visits) * 100, 2) if peak_visits else 0.0

    return {
        "store_id": store_id,
        "window_start": day_start.isoformat(),
        "window_end": day_end.isoformat(),
        "total_sessions": total_entries,
        "data_confidence": "low" if total_entries < 20 else "normal",
        "zones": zones,
    }
