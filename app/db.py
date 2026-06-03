"""Database layer — async SQLAlchemy engine, table definitions, session helper.

Works transparently with both Postgres (production) and SQLite (tests).
Idempotency enforced at schema level via PRIMARY KEY on event_id.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .config import APP_CONFIG

schema_registry = MetaData()
metadata = schema_registry

activity_log = Table(
    "events",
    schema_registry,
    Column("event_id", String(64), primary_key=True),
    Column("store_id", String(64), nullable=False),
    Column("camera_id", String(64), nullable=False),
    Column("visitor_id", String(64), nullable=False),
    Column("event_type", String(32), nullable=False),
    Column("timestamp", DateTime(timezone=True), nullable=False),
    Column("zone_id", String(64), nullable=True),
    Column("dwell_ms", Integer, nullable=False, default=0),
    Column("is_staff", Boolean, nullable=False, default=False),
    Column("confidence", Float, nullable=False),
    Column("metadata_json", JSON, nullable=False, default=dict),
    Index("ix_events_store_time", "store_id", "timestamp"),
    Index("ix_events_visitor", "visitor_id"),
    Index("ix_events_type_store_time", "event_type", "store_id", "timestamp"),
)

sales_ledger = Table(
    "pos_transactions",
    schema_registry,
    Column("transaction_id", String(64), primary_key=True),
    Column("store_id", String(64), nullable=False),
    Column("visitor_id", String(64), nullable=True),
    Column("timestamp", DateTime(timezone=True), nullable=False),
    Column("basket_value", Float, nullable=False),
    Column("items_count", Integer, nullable=False, default=0),
    Column("line_items", JSON, nullable=False, default=list),
    Index("ix_pos_store_time", "store_id", "timestamp"),
)


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _build_engine(url: str) -> AsyncEngine:
    # SQLite needs no pool sizing; Postgres gets a bounded pool.
    if url.startswith("sqlite"):
        return create_async_engine(url, future=True)
    return create_async_engine(url, future=True, pool_size=10, max_overflow=5)


def get_engine() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        _engine = _build_engine(APP_CONFIG.database_url)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


@asynccontextmanager
async def db_transaction() -> AsyncIterator[AsyncSession]:
    sf = get_session_factory()
    async with sf() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


async def create_all() -> None:
    """Bootstrap tables when they don't exist yet.

    Alembic handles migrations in production; this is a safety net
    so acceptance gates never trip on missing tables.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(schema_registry.create_all)


async def dispose() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


def override_database_url(url: str) -> None:
    """Test-only hook — swap the global URL and force engine recreation."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None
    object.__setattr__(APP_CONFIG, "database_url", url)
