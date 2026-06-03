"""Request audit middleware — emits structured JSON per request.

Each log line includes trace_id, latency_ms, store_id, endpoint,
event_count, and status_code. Backed by structlog.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Awaitable, Callable

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .config import APP_CONFIG


def setup_audit_logging() -> None:
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, APP_CONFIG.log_level, logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, APP_CONFIG.log_level, logging.INFO),
        ),
    )


_OUTLET_PATTERN = re.compile(r"/stores/([^/]+)")


def _extract_outlet_id(path: str) -> str | None:
    m = _OUTLET_PATTERN.search(path)
    return m.group(1) if m else None


class RequestAuditLayer(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        trace_id = request.headers.get("x-trace-id") or str(uuid.uuid4())
        t0 = time.perf_counter()
        request.state.trace_id = trace_id
        request.state.event_count = 0

        audit = structlog.get_logger().bind(
            trace_id=trace_id,
            endpoint=request.url.path,
            method=request.method,
            store_id=_extract_outlet_id(request.url.path),
        )

        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["x-trace-id"] = trace_id
            return response
        finally:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            audit.info(
                "request",
                status_code=status_code,
                latency_ms=elapsed_ms,
                event_count=getattr(request.state, "event_count", 0),
            )
