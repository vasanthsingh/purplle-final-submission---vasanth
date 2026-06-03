"""Batch event ingestion with idempotency and partial-success semantics.

- Up to 500 events per batch (oversized triggers 413).
- Dedup via ON CONFLICT DO NOTHING / INSERT OR IGNORE on event_id.
- Per-event validation failures are reported individually; the batch is not rejected wholesale.
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .config import APP_CONFIG
from .db import activity_log, db_transaction
from .models import BehaviourEvent, BatchResult, FailedRecord


class PayloadOverflow(ValueError):
    pass


def _event_to_row(evt: BehaviourEvent) -> dict[str, Any]:
    return {
        "event_id": str(evt.event_id),
        "store_id": evt.store_id,
        "camera_id": evt.camera_id,
        "visitor_id": evt.visitor_id,
        "event_type": evt.event_type.value,
        "timestamp": evt.timestamp,
        "zone_id": evt.zone_id,
        "dwell_ms": evt.dwell_ms,
        "is_staff": evt.is_staff,
        "confidence": evt.confidence,
        "metadata_json": evt.metadata,
    }


async def _insert_skip_dups(session: AsyncSession, rows: list[dict[str, Any]]) -> int:
    """Persist rows, silently skipping duplicates. Returns count of new rows."""
    if not rows:
        return 0

    ids = [r["event_id"] for r in rows]
    existing_q = select(activity_log.c.event_id).where(activity_log.c.event_id.in_(ids))
    existing = {r[0] for r in (await session.execute(existing_q)).all()}
    fresh_rows = [r for r in rows if r["event_id"] not in existing]

    if not fresh_rows:
        return 0

    dialect = session.bind.dialect.name if session.bind else ""
    stmt: Any
    if dialect == "postgresql":
        stmt = pg_insert(activity_log).values(fresh_rows).on_conflict_do_nothing(
            index_elements=[activity_log.c.event_id]
        )
    elif dialect == "sqlite":
        stmt = sqlite_insert(activity_log).values(fresh_rows).on_conflict_do_nothing(
            index_elements=[activity_log.c.event_id]
        )
    else:
        stmt = activity_log.insert().values(fresh_rows)
    await session.execute(stmt)
    return len(fresh_rows)


async def process_event_batch(raw_events: list[dict[str, Any]]) -> BatchResult:
    """Validate and persist a batch of events. Returns partial-success response."""
    if len(raw_events) > APP_CONFIG.batch_max_events:
        raise PayloadOverflow(
            f"batch size {len(raw_events)} exceeds max {APP_CONFIG.batch_max_events}"
        )
    if not raw_events:
        return BatchResult(accepted=0, duplicates=0, rejected=[])

    validated: list[BehaviourEvent] = []
    rejected: list[FailedRecord] = []
    for raw in raw_events:
        try:
            validated.append(BehaviourEvent.model_validate(raw))
        except ValidationError as ve:
            rejected.append(
                FailedRecord(
                    event_id=str(raw.get("event_id")) if isinstance(raw, dict) else None,
                    error=_format_validation_error(ve),
                )
            )

    if not validated:
        return BatchResult(accepted=0, duplicates=0, rejected=rejected)

    rows = [_event_to_row(evt) for evt in validated]
    async with db_transaction() as s:
        inserted = await _insert_skip_dups(s, rows)

    return BatchResult(
        accepted=inserted,
        duplicates=len(validated) - inserted,
        rejected=rejected,
    )


def _format_validation_error(exc: ValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {"msg": "invalid"}
    loc = ".".join(str(p) for p in first.get("loc", ()))
    return f"{loc}: {first.get('msg', 'invalid')}" if loc else str(first.get("msg", "invalid"))
