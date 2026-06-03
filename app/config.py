"""Runtime configuration loaded from environment variables with sensible defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AppConfig:
    database_url: str
    log_level: str
    stale_feed_minutes: int
    queue_spike_threshold: int
    queue_spike_duration_sec: int
    dead_zone_window_sec: int
    conversion_window_sec: int
    batch_max_events: int
    trailing_days: int

    @classmethod
    def from_env(cls) -> "AppConfig":
        # When DATABASE_URL is absent fall back to a local SQLite file
        # so that tests and quick local runs work without any setup.
        # docker-compose always supplies DATABASE_URL for production.
        fallback_db = "sqlite+aiosqlite:///./data/vortex.db"
        db_url = os.getenv("DATABASE_URL", fallback_db)
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif db_url.startswith("postgresql://"):
            db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
            
        return cls(
            database_url=db_url,
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            stale_feed_minutes=int(os.getenv("STALE_FEED_MINUTES", "10")),
            queue_spike_threshold=int(os.getenv("QUEUE_SPIKE_THRESHOLD", "7")),
            queue_spike_duration_sec=int(os.getenv("QUEUE_SPIKE_DURATION_SEC", "120")),
            dead_zone_window_sec=int(os.getenv("DEAD_ZONE_WINDOW_SEC", "1800")),
            conversion_window_sec=int(os.getenv("CONVERSION_WINDOW_SEC", "300")),
            batch_max_events=int(os.getenv("BATCH_MAX_EVENTS", "500")),
            trailing_days=int(os.getenv("TRAILING_DAYS", "7")),
        )


APP_CONFIG = AppConfig.from_env()
