"""Session-based conversion pipeline.

Stages: Entry → ZoneVisit → BillingQueue → Purchase.
Sessions are keyed by (store_id, visitor_id) within the day.
REENTRY events collapse into the same session (no double counting).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, select

from .db import activity_log, sales_ledger, db_transaction
from .metrics import _current_day_range


@dataclass(frozen=True)
class ConversionStep:
    name: str
    count: int
    drop_off_pct_from_prev: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.name,
            "count": self.count,
            "drop_off_from_prev_pct": round(self.drop_off_pct_from_prev, 2),
        }


async def build_conversion_pipeline(store_id: str, now: datetime | None = None) -> dict[str, Any]:
    day_start, day_end = _current_day_range(now)
    ev = activity_log.c
    sl = sales_ledger.c

    async with db_transaction() as s:
        # Fetch all non-staff events for this store today.
        rows = (await s.execute(
            select(ev.visitor_id, ev.event_type, ev.timestamp).where(
                and_(
                    ev.store_id == store_id,
                    ev.timestamp >= day_start,
                    ev.timestamp < day_end,
                    ev.is_staff.is_(False),
                )
            )
        )).all()
        pos_rows = (await s.execute(
            select(sl.timestamp).where(
                and_(sl.store_id == store_id, sl.timestamp >= day_start, sl.timestamp < day_end)
            )
        )).all()

    entered: set[str] = set()
    zone_visitors: set[str] = set()
    billing_visitors: set[str] = set()
    billing_joins: list[tuple[str, datetime]] = []

    for vid, etype, ts in rows:
        if etype in ("ENTRY", "REENTRY"):
            entered.add(vid)
        if etype in ("ZONE_ENTER",):
            zone_visitors.add(vid)
        if etype == "BILLING_QUEUE_JOIN":
            billing_visitors.add(vid)
            billing_joins.append((vid, ts))

    pos_timestamps = [r[0] for r in pos_rows]
    purchasers = set()
    from datetime import timedelta
    for vid, join_ts in billing_joins:
        if vid in purchasers:
            continue
        for pts in pos_timestamps:
            if timedelta(seconds=0) <= pts - join_ts <= timedelta(minutes=5):
                purchasers.add(vid)
                break

    # Enforce funnel monotonicity.
    zone_visitors &= entered
    billing_visitors &= entered
    purchasers &= billing_visitors

    stage_data = [
        ("Entry", len(entered)),
        ("ZoneVisit", len(zone_visitors)),
        ("BillingQueue", len(billing_visitors)),
        ("Purchase", len(purchasers)),
    ]
    steps: list[ConversionStep] = []
    prev_count = 0
    for idx, (name, cnt) in enumerate(stage_data):
        if idx == 0:
            drop = 0.0
        else:
            drop = ((prev_count - cnt) / prev_count * 100.0) if prev_count > 0 else 0.0
        steps.append(ConversionStep(name=name, count=cnt, drop_off_pct_from_prev=drop))
        prev_count = cnt

    conversion_rate = (len(purchasers) / len(entered)) if entered else 0.0

    return {
        "store_id": store_id,
        "window_start": day_start.isoformat(),
        "window_end": day_end.isoformat(),
        "total_sessions": len(entered),
        "conversion_rate": round(conversion_rate, 4),
        "stages": [s.to_dict() for s in steps],
    }
