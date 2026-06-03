"""Outlet-level metrics computation.

Every query filters out is_staff=true. Numeric outputs default to 0
(never null) so the endpoint is safe even with an empty store.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, select

from .config import APP_CONFIG
from .db import activity_log, sales_ledger, db_transaction


def _current_day_range(now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    return day_start, day_end


@dataclass(frozen=True)
class OutletSnapshot:
    store_id: str
    window_start: str
    window_end: str
    unique_visitors: int
    conversion_rate: float
    abandonment_rate: float
    avg_dwell_per_zone_ms: dict[str, float]
    current_queue_depth: int
    pos_transactions: int
    top_brands: dict[str, int]
    top_departments: dict[str, int]
    staff_count: int
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "store_id": self.store_id,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "unique_visitors": self.unique_visitors,
            "conversion_rate": round(self.conversion_rate, 4),
            "abandonment_rate": round(self.abandonment_rate, 4),
            "avg_dwell_per_zone_ms": self.avg_dwell_per_zone_ms,
            "current_queue_depth": self.current_queue_depth,
            "pos_transactions": self.pos_transactions,
            "top_brands": self.top_brands,
            "top_departments": self.top_departments,
            "staff_count": self.staff_count,
            "generated_at": self.generated_at,
        }


async def generate_outlet_snapshot(store_id: str, now: datetime | None = None) -> OutletSnapshot:
    from collections import Counter
    day_start, day_end = _current_day_range(now)
    now = now or datetime.now(timezone.utc)
    ev = activity_log.c
    sl = sales_ledger.c

    async with db_transaction() as s:
        # Unique non-staff visitors entering today.
        uv_q = select(func.count(func.distinct(ev.visitor_id))).where(
            and_(
                ev.store_id == store_id,
                ev.timestamp >= day_start,
                ev.timestamp < day_end,
                ev.event_type == "ENTRY",
                ev.is_staff.is_(False),
            )
        )
        unique_visitors = int((await s.execute(uv_q)).scalar() or 0)

        # Staff count tracked separately for transparency.
        staff_q = select(func.count(func.distinct(ev.visitor_id))).where(
            and_(
                ev.store_id == store_id,
                ev.timestamp >= day_start,
                ev.timestamp < day_end,
                ev.event_type == "ENTRY",
                ev.is_staff.is_(True),
            )
        )
        staff_count = int((await s.execute(staff_q)).scalar() or 0)

        # Billing queue join events with timestamps for conversion correlation.
        bq_q = select(ev.visitor_id, ev.timestamp).where(
            and_(
                ev.store_id == store_id,
                ev.timestamp >= day_start,
                ev.timestamp < day_end,
                ev.event_type == "BILLING_QUEUE_JOIN",
                ev.is_staff.is_(False),
            )
        )
        bq_rows = (await s.execute(bq_q)).all()

        # Queue abandonment count.
        abandon_q = select(func.count()).where(
            and_(
                ev.store_id == store_id,
                ev.timestamp >= day_start,
                ev.timestamp < day_end,
                ev.event_type == "BILLING_QUEUE_ABANDON",
                ev.is_staff.is_(False),
            )
        )
        abandons = int((await s.execute(abandon_q)).scalar() or 0)
        joins = len(bq_rows)
        abandonment_rate = (abandons / joins) if joins > 0 else 0.0

        # POS transactions for today.
        pos_q = select(sl.timestamp, sl.line_items).where(
            and_(sl.store_id == store_id, sl.timestamp >= day_start, sl.timestamp < day_end)
        )
        pos_rows = (await s.execute(pos_q)).all()
        pos_ts_list = [r[0] for r in pos_rows]

        # Top brands and departments from line items.
        brand_counter = Counter()
        dept_counter = Counter()
        for r in pos_rows:
            line_items = r[1] or []
            if isinstance(line_items, str):
                import json
                line_items = json.loads(line_items)
            for item in line_items:
                if isinstance(item, dict):
                    b = item.get("brand_name")
                    d = item.get("dep_name")
                    if b: brand_counter[b] += 1
                    if d: dept_counter[d] += 1

        top_brands = dict(brand_counter.most_common(5))
        top_departments = dict(dept_counter.most_common(5))

        # Conversion via 5-minute window correlation.
        converted = set()
        for vid, join_ts in bq_rows:
            if vid in converted:
                continue
            for pts in pos_ts_list:
                if timedelta(seconds=0) <= pts - join_ts <= timedelta(minutes=5):
                    converted.add(vid)
                    break
        conversion_rate = (len(converted) / unique_visitors) if unique_visitors > 0 else 0.0

        # Average dwell per zone from ZONE_DWELL events.
        dwell_q = select(ev.zone_id, func.avg(ev.dwell_ms)).where(
            and_(
                ev.store_id == store_id,
                ev.timestamp >= day_start,
                ev.timestamp < day_end,
                ev.event_type == "ZONE_DWELL",
                ev.is_staff.is_(False),
                ev.zone_id.isnot(None),
            )
        ).group_by(ev.zone_id)
        avg_dwell = {
            row[0]: round(float(row[1] or 0), 2)
            for row in (await s.execute(dwell_q)).all()
        }

        # Current queue depth from recent metadata.
        cutoff = now - timedelta(minutes=5)
        qd_q = select(ev.metadata_json).where(
            and_(
                ev.store_id == store_id,
                ev.timestamp >= cutoff,
                ev.event_type == "BILLING_QUEUE_JOIN",
                ev.is_staff.is_(False),
            )
        )
        rows = (await s.execute(qd_q)).all()
        depths = [int(r[0].get("queue_depth", 0)) for r in rows if isinstance(r[0], dict)]
        current_queue_depth = max(depths) if depths else 0

    return OutletSnapshot(
        store_id=store_id,
        window_start=day_start.isoformat(),
        window_end=day_end.isoformat(),
        unique_visitors=unique_visitors,
        conversion_rate=conversion_rate,
        abandonment_rate=abandonment_rate,
        avg_dwell_per_zone_ms=avg_dwell,
        current_queue_depth=current_queue_depth,
        pos_transactions=len(pos_ts_list),
        top_brands=top_brands,
        top_departments=top_departments,
        staff_count=staff_count,
        generated_at=now.isoformat(),
    )

_ = APP_CONFIG  # keep import alive for future tuning
